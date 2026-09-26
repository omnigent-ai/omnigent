"""Headless stub Wayland compositor: enough wl_*/xdg_wm_base for Chromium to open windows
(nothing is displayed), zero-sized geometry rejected unless ``--lenient-geometry``, JSON-line
logs on stdout, and ``maximize``/``unmaximize``/``quit`` command files under ``--control DIR``."""

from __future__ import annotations

import argparse
import json
import os
import signal
import struct
import sys
import time

from pywayland import ffi, lib
from pywayland.protocol.wayland import (
    WlBuffer,
    WlCallback,
    WlCompositor,
    WlDataDevice,
    WlDataDeviceManager,
    WlDataSource,
    WlKeyboard,
    WlOutput,
    WlPointer,
    WlRegion,
    WlSeat,
    WlShm,
    WlShmPool,
    WlSubcompositor,
    WlSubsurface,
    WlSurface,
    WlTouch,
)
from pywayland.protocol.xdg_shell import (
    XdgPopup,
    XdgPositioner,
    XdgSurface,
    XdgToplevel,
    XdgWmBase,
)
from pywayland.protocol_core.message import Message
from pywayland.protocol_core.resource import Resource
from pywayland.scanner.argument import ArgumentType
from pywayland.server import Display

XDG_STATE_MAXIMIZED = 1
XDG_STATE_ACTIVATED = 4
XDG_WM_CAPABILITIES = (1, 2, 3, 4)  # window_menu, maximize, fullscreen, minimize
WL_OUTPUT_MODE_CURRENT_PREFERRED = 3


def _resource_from_ptr(obj_ptr):
    handle = lib.wl_resource_get_user_data(ffi.cast("struct wl_resource *", obj_ptr))
    return None if handle == ffi.NULL else ffi.from_handle(handle)


def _server_c_to_arguments(self, args_ptr):
    # pywayland decodes request arguments with client-side (proxy) semantics;
    # server-side new_id is a bare id and objects are wl_resources.
    out = []
    for i, argument in enumerate(self.arguments):
        arg = args_ptr[i]
        kind = argument.argument_type
        if kind == ArgumentType.Int:
            out.append(arg.i)
        elif kind == ArgumentType.Uint:
            out.append(arg.u)
        elif kind == ArgumentType.Fixed:
            out.append(lib.wl_fixed_to_double(arg.f))
        elif kind == ArgumentType.FileDescriptor:
            out.append(arg.h)
        elif kind == ArgumentType.String:
            out.append(None if arg.s == ffi.NULL else ffi.string(arg.s).decode())
        elif kind == ArgumentType.Object:
            out.append(None if arg.o == ffi.NULL else _resource_from_ptr(arg.o))
        elif kind == ArgumentType.NewId:
            out.append(arg.n)
        elif kind == ArgumentType.Array:
            out.append(bytes(ffi.buffer(arg.a.data, arg.a.size)))
        else:
            raise ValueError(f"unsupported argument type {kind}")
    return out


Message.c_to_arguments = _server_c_to_arguments

_resource_init = Resource.__init__


def _server_resource_init(self, client, version=None, id=0):
    _resource_init(self, client, version, id)
    # libwayland hands the dispatcher the *implementation* pointer, which
    # pywayland leaves NULL; point it at the handle its dispatcher expects.
    lib.wl_resource_set_dispatcher(
        self._ptr, lib.dispatcher_func, self._handle, self._handle, lib.resource_destroy_func
    )


Resource.__init__ = _server_resource_init


def emit(resource, name, *values):
    """Post ``name`` on ``resource`` (pywayland's own array marshalling is broken)."""
    if resource._ptr is None:
        return
    events = resource.interface.events
    opcode = next(i for i, message in enumerate(events) if message.name == name)
    message = events[opcode]
    args = ffi.new("union wl_argument[]", max(1, len(message.arguments)))
    keep = []
    for i, (argument, value) in enumerate(zip(message.arguments, values, strict=True)):
        kind = argument.argument_type
        if kind == ArgumentType.Int:
            args[i].i = value
        elif kind == ArgumentType.Uint:
            args[i].u = value
        elif kind == ArgumentType.Fixed:
            args[i].f = lib.wl_fixed_from_double(float(value))
        elif kind == ArgumentType.String:
            text = ffi.new("char[]", value.encode())
            keep.append(text)
            args[i].s = text
        elif kind == ArgumentType.Object:
            args[i].o = ffi.NULL if value is None else ffi.cast("struct wl_object *", value._ptr)
        elif kind == ArgumentType.Array:
            data = ffi.new("char[]", max(1, len(value)))
            ffi.buffer(data)[: len(value)] = value
            array = ffi.new("struct wl_array *")
            array.size = array.alloc = len(value)
            array.data = data
            keep.extend((data, array))
            args[i].a = array
        elif kind == ArgumentType.FileDescriptor:
            args[i].h = value
        else:
            raise ValueError(f"unsupported argument type {kind}")
    lib.wl_resource_post_event_array(resource._ptr, opcode, args)


def pack_states(*states):
    return struct.pack(f"<{len(states)}I", *states)


def client_key(resource):
    return int(ffi.cast("uintptr_t", lib.wl_resource_get_client(resource._ptr)))


class State:
    """Per-resource bookkeeping attached to a wl_resource."""

    def __init__(self, **fields):
        self.__dict__.update(fields)


class StubCompositor:
    def __init__(self, args):
        self.width = args.width
        self.height = args.height
        self.strict = not args.lenient_geometry
        self.control = args.control
        self.display = Display()
        self.socket = self.display.add_socket(args.socket)
        self.loop = self.display.get_event_loop()
        self.keep = []
        self.live = {}
        self.states = {}
        self.outputs_by_client = {}
        self.toplevels = {}
        self.pending_frames = []
        self.pending_releases = []
        self.quit = False
        self.start = time.monotonic()

    # -- logging / bookkeeping ------------------------------------------------

    def log(self, event, **fields):
        record = {"t": round(time.monotonic() - self.start, 3), "event": event, **fields}
        sys.stdout.write(json.dumps(record) + "\n")
        sys.stdout.flush()

    def track(self, resource, **fields):
        self.live[id(resource)] = resource
        self.states[id(resource)] = State(**fields)
        resource.dispatcher.destructor = self._on_destroy
        return resource

    def state_of(self, resource):
        return self.states.get(id(resource))

    def _on_destroy(self, resource):
        key = id(resource)
        state = self.states.pop(key, None)
        self.live.pop(key, None)
        toplevel = self.toplevels.pop(key, None)
        if toplevel is not None:
            self.log("toplevel_destroyed", title=toplevel.title, app_id=toplevel.app_id)
        if state is not None and getattr(state, "kind", None) == "output":
            outputs = self.outputs_by_client.get(state.client, [])
            if resource in outputs:
                outputs.remove(resource)
        # libwayland frees the wl_resource right after this callback.
        resource._ptr = None

    def child(self, owner, interface, new_id, **fields):
        resource = interface.resource_class(
            lib.wl_resource_get_client(owner._ptr), owner.version, new_id
        )
        return self.track(resource, **fields)

    def destroy_request(self, resource):
        resource.destroy()

    def add_global(self, interface, version, bind):
        global_object = interface.global_class(self.display, version)
        global_object.bind_func = bind
        self.keep.append(global_object)

    def noop(self, _resource, *_args):
        return None

    def install(self, resource, handlers):
        for name, handler in handlers.items():
            resource.dispatcher[name] = handler
        if "destroy" in resource.dispatcher._names and resource.dispatcher["destroy"] is None:
            resource.dispatcher["destroy"] = self.destroy_request

    # -- globals ------------------------------------------------------------------

    def create_globals(self):
        self.add_global(WlCompositor, 4, self.bind_compositor)
        self.add_global(WlSubcompositor, 1, self.bind_subcompositor)
        self.add_global(WlShm, 1, self.bind_shm)
        self.add_global(WlOutput, 4, self.bind_output)
        self.add_global(WlSeat, 5, self.bind_seat)
        self.add_global(WlDataDeviceManager, 3, self.bind_data_device_manager)
        self.add_global(XdgWmBase, 5, self.bind_wm_base)

    def bind_compositor(self, resource):
        self.track(resource)
        self.log("bind", interface="wl_compositor", version=resource.version)
        self.install(
            resource,
            {"create_surface": self.create_surface, "create_region": self.create_region},
        )

    def create_surface(self, resource, new_id):
        surface = self.child(
            resource,
            WlSurface,
            new_id,
            kind="surface",
            buffer=None,
            frames=[],
            xdg=None,
            entered=False,
        )
        self.install(
            surface,
            {
                "attach": self.surface_attach,
                "frame": self.surface_frame,
                "commit": self.surface_commit,
                "damage": self.noop,
                "damage_buffer": self.noop,
                "set_opaque_region": self.noop,
                "set_input_region": self.noop,
                "set_buffer_transform": self.noop,
                "set_buffer_scale": self.noop,
                "offset": self.noop,
            },
        )

    def create_region(self, resource, new_id):
        region = self.child(resource, WlRegion, new_id, kind="region")
        self.install(region, {"add": self.noop, "subtract": self.noop})

    def surface_attach(self, surface, buffer, _x, _y):
        self.state_of(surface).buffer = buffer

    def surface_frame(self, surface, new_id):
        callback = self.child(surface, WlCallback, new_id, kind="callback")
        self.state_of(surface).frames.append(callback)

    def surface_commit(self, surface):
        state = self.state_of(surface)
        if state.buffer is not None:
            self.pending_releases.append(state.buffer)
            state.buffer = None
        self.pending_frames.extend(state.frames)
        state.frames = []
        if not state.entered:
            for output in self.outputs_by_client.get(client_key(surface), []):
                emit(surface, "enter", output)
            state.entered = True
        xdg = state.xdg
        if xdg is not None and not self.state_of(xdg).configured:
            self.initial_configure(xdg)

    def bind_subcompositor(self, resource):
        self.track(resource)
        self.install(resource, {"get_subsurface": self.get_subsurface})

    def get_subsurface(self, resource, new_id, _surface, _parent):
        subsurface = self.child(resource, WlSubsurface, new_id, kind="subsurface")
        self.install(
            subsurface,
            {
                "set_position": self.noop,
                "place_above": self.noop,
                "place_below": self.noop,
                "set_sync": self.noop,
                "set_desync": self.noop,
            },
        )

    def bind_shm(self, resource):
        self.track(resource)
        self.install(resource, {"create_pool": self.create_pool})
        emit(resource, "format", WlShm.format.argb8888.value)
        emit(resource, "format", WlShm.format.xrgb8888.value)

    def create_pool(self, resource, new_id, fd, _size):
        os.close(fd)
        pool = self.child(resource, WlShmPool, new_id, kind="pool")
        self.install(pool, {"create_buffer": self.create_buffer, "resize": self.noop})

    def create_buffer(self, pool, new_id, _offset, width, height, _stride, _fmt):
        self.child(pool, WlBuffer, new_id, kind="buffer", width=width, height=height)

    def bind_output(self, resource):
        key = client_key(resource)
        self.track(resource, kind="output", client=key)
        self.outputs_by_client.setdefault(key, []).append(resource)
        self.install(resource, {"release": self.destroy_request})
        emit(resource, "geometry", 0, 0, 509, 286, 0, "Stub", "Headless", 0)
        emit(resource, "mode", WL_OUTPUT_MODE_CURRENT_PREFERRED, self.width, self.height, 60000)
        if resource.version >= 2:
            emit(resource, "scale", 1)
        if resource.version >= 4:
            emit(resource, "name", "STUB-1")
            emit(resource, "description", "Stub headless output")
        if resource.version >= 2:
            emit(resource, "done")

    def bind_seat(self, resource):
        self.track(resource)
        self.log("bind", interface="wl_seat", version=resource.version)
        self.install(
            resource,
            {
                "get_pointer": self.get_pointer,
                "get_keyboard": self.get_keyboard,
                "get_touch": self.get_touch,
                "release": self.destroy_request,
            },
        )
        if resource.version >= 2:
            emit(resource, "name", "seat0")
        emit(resource, "capabilities", 0)

    def get_pointer(self, seat, new_id):
        pointer = self.child(seat, WlPointer, new_id, kind="pointer")
        self.install(pointer, {"set_cursor": self.noop, "release": self.destroy_request})

    def get_keyboard(self, seat, new_id):
        keyboard = self.child(seat, WlKeyboard, new_id, kind="keyboard")
        self.install(keyboard, {"release": self.destroy_request})

    def get_touch(self, seat, new_id):
        touch = self.child(seat, WlTouch, new_id, kind="touch")
        self.install(touch, {"release": self.destroy_request})

    def bind_data_device_manager(self, resource):
        self.track(resource)
        self.install(
            resource,
            {
                "create_data_source": self.create_data_source,
                "get_data_device": self.get_data_device,
            },
        )

    def create_data_source(self, resource, new_id):
        source = self.child(resource, WlDataSource, new_id, kind="data_source")
        self.install(source, {"offer": self.noop, "set_actions": self.noop})

    def get_data_device(self, resource, new_id, _seat):
        device = self.child(resource, WlDataDevice, new_id, kind="data_device")
        self.install(
            device,
            {"start_drag": self.noop, "set_selection": self.noop, "release": self.destroy_request},
        )

    # -- xdg-shell ----------------------------------------------------------------

    def bind_wm_base(self, resource):
        self.track(resource)
        self.log("bind", interface="xdg_wm_base", version=resource.version)
        self.install(
            resource,
            {
                "create_positioner": self.create_positioner,
                "get_xdg_surface": self.get_xdg_surface,
                "pong": self.noop,
            },
        )

    def create_positioner(self, resource, new_id):
        positioner = self.child(resource, XdgPositioner, new_id, kind="positioner", size=(1, 1))
        self.install(
            positioner,
            {
                "set_size": self.positioner_set_size,
                "set_anchor_rect": self.noop,
                "set_anchor": self.noop,
                "set_gravity": self.noop,
                "set_constraint_adjustment": self.noop,
                "set_offset": self.noop,
                "set_reactive": self.noop,
                "set_parent_size": self.noop,
                "set_parent_configure": self.noop,
            },
        )

    def positioner_set_size(self, positioner, width, height):
        self.state_of(positioner).size = (width, height)

    def get_xdg_surface(self, resource, new_id, surface):
        xdg = self.child(
            resource,
            XdgSurface,
            new_id,
            kind="xdg_surface",
            surface=surface,
            toplevel=None,
            popup=None,
            configured=False,
            geometry=None,
        )
        self.state_of(surface).xdg = xdg
        self.install(
            xdg,
            {
                "get_toplevel": self.get_toplevel,
                "get_popup": self.get_popup,
                "set_window_geometry": self.set_window_geometry,
                "ack_configure": self.noop,
            },
        )

    def get_toplevel(self, xdg, new_id):
        toplevel = self.child(
            xdg,
            XdgToplevel,
            new_id,
            kind="toplevel",
            xdg=xdg,
            title="",
            app_id="",
            parent=None,
            maximized=False,
        )
        self.state_of(xdg).toplevel = toplevel
        self.toplevels[id(toplevel)] = self.state_of(toplevel)
        self.install(
            toplevel,
            {
                "set_parent": self.toplevel_set_parent,
                "set_title": self.toplevel_set_title,
                "set_app_id": self.toplevel_set_app_id,
                "show_window_menu": self.noop,
                "move": self.noop,
                "resize": self.noop,
                "set_max_size": self.noop,
                "set_min_size": self.noop,
                "set_maximized": lambda res: self.maximize(res, source="client"),
                "unset_maximized": lambda res: self.unmaximize(res, source="client"),
                "set_fullscreen": self.noop,
                "unset_fullscreen": self.noop,
                "set_minimized": self.noop,
            },
        )
        self.log("toplevel_created", xdg_surface=xdg.get_id())

    def toplevel_set_parent(self, toplevel, parent):
        self.state_of(toplevel).parent = parent
        self.log(
            "set_parent",
            xdg_surface=self.state_of(toplevel).xdg.get_id(),
            parent_xdg_surface=None if parent is None else self.state_of(parent).xdg.get_id(),
        )

    def toplevel_set_title(self, toplevel, title):
        self.state_of(toplevel).title = title

    def toplevel_set_app_id(self, toplevel, app_id):
        self.state_of(toplevel).app_id = app_id

    def get_popup(self, xdg, new_id, _parent, positioner):
        popup = self.child(xdg, XdgPopup, new_id, kind="popup", xdg=xdg, positioner=positioner)
        self.state_of(xdg).popup = popup
        self.install(popup, {"grab": self.noop, "reposition": self.popup_reposition})

    def popup_reposition(self, popup, positioner, token):
        self.state_of(popup).positioner = positioner
        emit(popup, "repositioned", token)
        self.configure_popup(popup)

    def describe(self, xdg):
        state = self.state_of(xdg)
        toplevel = state.toplevel
        if toplevel is None:
            return {"xdg_surface": xdg.get_id(), "role": "popup" if state.popup else "none"}
        tl = self.state_of(toplevel)
        return {
            "xdg_surface": xdg.get_id(),
            "role": "toplevel",
            "title": tl.title,
            "app_id": tl.app_id,
            "child": tl.parent is not None,
        }

    def set_window_geometry(self, xdg, x, y, width, height):
        self.state_of(xdg).geometry = (x, y, width, height)
        info = self.describe(xdg)
        self.log("set_window_geometry", x=x, y=y, width=width, height=height, **info)
        if width <= 0 or height <= 0:
            self.log(
                "invalid_window_geometry", width=width, height=height, strict=self.strict, **info
            )
            if self.strict:
                xdg._post_error(
                    XdgSurface.error.invalid_size.value,
                    "xdg_surface.set_window_geometry: size must be positive, "
                    f"got {width}x{height}",
                )

    def initial_configure(self, xdg):
        state = self.state_of(xdg)
        if state.toplevel is not None:
            toplevel = state.toplevel
            if toplevel.version >= 5:
                emit(toplevel, "wm_capabilities", pack_states(*XDG_WM_CAPABILITIES))
            if toplevel.version >= 4:
                emit(toplevel, "configure_bounds", self.width, self.height)
            emit(toplevel, "configure", 0, 0, pack_states(XDG_STATE_ACTIVATED))
            serial = self.display.next_serial()
            emit(xdg, "configure", serial)
            state.configured = True
            self.log(
                "configure",
                width=0,
                height=0,
                states=["activated"],
                serial=serial,
                **self.describe(xdg),
            )
        elif state.popup is not None:
            self.configure_popup(state.popup)
            state.configured = True

    def configure_popup(self, popup):
        state = self.state_of(popup)
        width, height = self.state_of(state.positioner).size
        emit(popup, "configure", 0, 0, max(1, width), max(1, height))
        emit(state.xdg, "configure", self.display.next_serial())

    def maximize(self, toplevel, source):
        state = self.state_of(toplevel)
        state.maximized = True
        emit(
            toplevel,
            "configure",
            self.width,
            self.height,
            pack_states(XDG_STATE_MAXIMIZED, XDG_STATE_ACTIVATED),
        )
        serial = self.display.next_serial()
        emit(state.xdg, "configure", serial)
        self.log(
            "configure",
            width=self.width,
            height=self.height,
            states=["maximized", "activated"],
            serial=serial,
            source=source,
            **self.describe(state.xdg),
        )

    def unmaximize(self, toplevel, source):
        state = self.state_of(toplevel)
        state.maximized = False
        emit(toplevel, "configure", 0, 0, pack_states(XDG_STATE_ACTIVATED))
        serial = self.display.next_serial()
        emit(state.xdg, "configure", serial)
        self.log(
            "configure",
            width=0,
            height=0,
            states=["activated"],
            serial=serial,
            source=source,
            **self.describe(state.xdg),
        )

    # -- main loop ----------------------------------------------------------------

    def tick(self):
        now = int((time.monotonic() - self.start) * 1000) & 0xFFFFFFFF
        frames, self.pending_frames = self.pending_frames, []
        for callback in frames:
            if callback._ptr is not None:
                emit(callback, "done", now)
                callback.destroy()
        releases, self.pending_releases = self.pending_releases, []
        for buffer in releases:
            emit(buffer, "release")
        self.poll_control()

    def target_toplevels(self, selector):
        """Toplevels a control command applies to: ``{"title": ...}`` picks by title,
        ``{"all": true}`` every toplevel; otherwise the largest non-child toplevel
        stands in for the focused window a real compositor would act on."""
        candidates = [
            (key, state)
            for key, state in self.toplevels.items()
            if state.parent is None and state.xdg._ptr is not None and key in self.live
        ]
        title = selector.get("title")
        if title is not None:
            return [self.live[key] for key, state in candidates if state.title == title]
        if selector.get("all"):
            return [self.live[key] for key, _ in candidates]

        def area(entry):
            geometry = self.state_of(entry[1].xdg).geometry
            return 0 if geometry is None else geometry[2] * geometry[3]

        return [self.live[max(candidates, key=area)[0]]] if candidates else []

    def poll_control(self):
        if not self.control:
            return
        for command in ("maximize", "unmaximize", "quit"):
            path = os.path.join(self.control, command)
            if not os.path.exists(path):
                continue
            with open(path, encoding="utf-8") as body:
                text = body.read().strip()
            selector = json.loads(text) if text else {}
            os.replace(path, path + ".taken")
            affected = []
            if command in ("maximize", "unmaximize"):
                for toplevel in self.target_toplevels(selector):
                    if command == "maximize":
                        self.maximize(toplevel, source="compositor")
                    else:
                        self.unmaximize(toplevel, source="compositor")
                    affected.append(self.describe(self.state_of(toplevel).xdg))
            elif command == "quit":
                self.quit = True
            self.log("control", command=command, selector=selector, affected=affected)
            with open(path + ".done", "w", encoding="utf-8") as done:
                json.dump({"command": command, "affected": affected}, done)

    def run(self):
        self.create_globals()
        self.log(
            "ready",
            socket=self.socket,
            width=self.width,
            height=self.height,
            strict_geometry=self.strict,
        )
        if self.control:
            os.makedirs(self.control, exist_ok=True)
            with open(os.path.join(self.control, "ready"), "w", encoding="utf-8") as ready:
                json.dump({"socket": self.socket}, ready)
        while not self.quit:
            self.display.flush_clients()
            self.loop.dispatch(16)
            self.tick()
        self.display.flush_clients()
        self.log("exit")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--socket", default=None, help="WAYLAND_DISPLAY socket name (default: auto)"
    )
    parser.add_argument(
        "--control", default=None, help="directory polled for maximize/unmaximize/quit files"
    )
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument(
        "--lenient-geometry",
        action="store_true",
        help="accept a zero-sized set_window_geometry instead of raising invalid_size",
    )
    args = parser.parse_args()
    compositor = StubCompositor(args)

    def stop(_signum, _frame):
        compositor.quit = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    compositor.run()


if __name__ == "__main__":
    main()
