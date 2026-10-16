"""Dateutil recurrence iteration with period and inner-search work limits.

Adapted from python-dateutil 2.9.0.post0, dateutil/rrule.py. Only the iterator is
copied so each search can bound work without modifying shared dependency state.
The dateutil parser and calendar masks remain the source of recurrence semantics.
"""

# dateutil - Extensions to the standard Python datetime module.
#
# Copyright (c) 2003-2011 - Gustavo Niemeyer <gustavo@niemeyer.net>
# Copyright (c) 2012-2014 - Tomi Pieviläinen <tomi.pievilainen@iki.fi>
# Copyright (c) 2014-2016 - Yaron de Leeuw <me@jarondl.net>
# Copyright (c) 2015-     - Paul Ganssle <paul@ganssle.io>
# Copyright (c) 2015-     - dateutil contributors (see AUTHORS file)
#
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
#     * Redistributions of source code must retain the above copyright notice,
#       this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright notice,
#       this list of conditions and the following disclaimer in the documentation
#       and/or other materials provided with the distribution.
#     * Neither the name of the copyright holder nor the names of its
#       contributors may be used to endorse or promote products derived from
#       this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR
# PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF
# LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING
# NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import calendar
import datetime
from collections.abc import Iterator
from math import gcd
from time import monotonic
from typing import Any, cast

from dateutil.rrule import (  # type: ignore[import-untyped]
    DAILY,
    HOURLY,
    MINUTELY,
    MONTHLY,
    SECONDLY,
    WEEKLY,
    YEARLY,
    _iterinfo,
    rrule,
)


def iter_bounded(
    rule: rrule, *, max_steps: int, max_seconds: float
) -> Iterator[datetime.datetime]:
    remaining = max_steps
    deadline = monotonic() + max_seconds

    def spend(steps: int = 1) -> None:
        nonlocal remaining
        remaining -= steps
        if remaining < 0:
            raise ValueError("RRULE search exceeds the supported work limit")
        if monotonic() >= deadline:
            raise ValueError("RRULE search exceeds the supported time limit")

    # Public dateutil stubs omit the iterator state and mask types.
    state = cast(Any, rule)
    year, month, day, hour, minute, second, weekday, _yearday, _ = state._dtstart.timetuple()

    # Some local variables to speed things up a bit
    freq = state._freq
    interval = state._interval
    wkst = state._wkst
    until = state._until
    local_until = until.astimezone(state._dtstart.tzinfo) if until is not None else None
    bymonth = state._bymonth
    byweekno = state._byweekno
    byyearday = state._byyearday
    byweekday = state._byweekday
    byeaster = state._byeaster
    bymonthday = state._bymonthday
    bynmonthday = state._bynmonthday
    bysetpos = state._bysetpos
    byhour = state._byhour
    byminute = state._byminute
    bysecond = state._bysecond

    ii = cast(Any, _iterinfo(rule))
    ii.rebuild(year, month)

    getdayset = {
        YEARLY: ii.ydayset,
        MONTHLY: ii.mdayset,
        WEEKLY: ii.wdayset,
        DAILY: ii.ddayset,
        HOURLY: ii.ddayset,
        MINUTELY: ii.ddayset,
        SECONDLY: ii.ddayset,
    }[freq]

    gettimeset = ii.htimeset
    if freq < HOURLY:
        timeset = state._timeset
    else:
        gettimeset = {HOURLY: ii.htimeset, MINUTELY: ii.mtimeset, SECONDLY: ii.stimeset}[freq]
        if (
            (freq >= HOURLY and state._byhour and hour not in state._byhour)
            or (freq >= MINUTELY and state._byminute and minute not in state._byminute)
            or (freq >= SECONDLY and state._bysecond and second not in state._bysecond)
        ):
            timeset = ()
        else:
            timeset = gettimeset(hour, minute, second)

    total = 0
    count = state._count
    while True:
        spend()
        # UNTIL must also stop periods whose filters produce no candidates.
        period = (year, month if freq != YEARLY else 1, day if freq >= WEEKLY else 1)
        if local_until is not None and period > (
            local_until.year,
            local_until.month,
            local_until.day,
        ):
            return
        # Get dayset with the right frequency
        dayset, start, end = getdayset(year, month, day)

        spend(end - start)
        filtered = False
        for i in dayset[start:end]:
            if (
                (bymonth and ii.mmask[i] not in bymonth)
                or (byweekno and not ii.wnomask[i])
                or (byweekday and ii.wdaymask[i] not in byweekday)
                or (ii.nwdaymask and not ii.nwdaymask[i])
                or (byeaster and not ii.eastermask[i])
                or (
                    (bymonthday or bynmonthday)
                    and ii.mdaymask[i] not in bymonthday
                    and ii.nmdaymask[i] not in bynmonthday
                )
                or (
                    byyearday
                    and (
                        (
                            i < ii.yearlen
                            and i + 1 not in byyearday
                            and -ii.yearlen + i not in byyearday
                        )
                        or (
                            i >= ii.yearlen
                            and i + 1 - ii.yearlen not in byyearday
                            and -ii.nextyearlen + i - ii.yearlen not in byyearday
                        )
                    )
                )
            ):
                dayset[i] = None
                filtered = True

        # Output results
        if bysetpos and timeset:
            poslist = []
            days = [i for i in dayset[start:end] if i is not None]
            for pos in bysetpos:
                spend()
                if pos < 0:
                    daypos, timepos = divmod(pos, len(timeset))
                else:
                    daypos, timepos = divmod(pos - 1, len(timeset))
                try:
                    i = days[daypos]
                    time = timeset[timepos]
                except IndexError:
                    pass
                else:
                    date = datetime.date.fromordinal(ii.yearordinal + i)
                    res = datetime.datetime.combine(date, time)
                    if res not in poslist:
                        poslist.append(res)
            poslist.sort()
            for res in poslist:
                spend()
                if until and res > until:
                    state._len = total
                    return
                elif res >= state._dtstart:
                    if count is not None:
                        count -= 1
                        if count < 0:
                            state._len = total
                            return
                    total += 1
                    yield res
        else:
            for i in dayset[start:end]:
                if i is not None:
                    date = datetime.date.fromordinal(ii.yearordinal + i)
                    for candidate_time in timeset:
                        spend()
                        res = datetime.datetime.combine(date, candidate_time)
                        if until and res > until:
                            state._len = total
                            return
                        elif res >= state._dtstart:
                            if count is not None:
                                count -= 1
                                if count < 0:
                                    state._len = total
                                    return

                            total += 1
                            yield res

        # Handle frequency and interval
        fixday = False
        if freq == YEARLY:
            year += interval
            if year > datetime.MAXYEAR:
                state._len = total
                return
            ii.rebuild(year, month)
        elif freq == MONTHLY:
            month += interval
            if month > 12:
                div, mod = divmod(month, 12)
                month = mod
                year += div
                if month == 0:
                    month = 12
                    year -= 1
                if year > datetime.MAXYEAR:
                    state._len = total
                    return
            ii.rebuild(year, month)
        elif freq == WEEKLY:
            if wkst > weekday:
                day += -(weekday + 1 + (6 - wkst)) + state._interval * 7
            else:
                day += -(weekday - wkst) + state._interval * 7
            weekday = wkst
            fixday = True
        elif freq == DAILY:
            day += interval
            fixday = True
        elif freq == HOURLY:
            if filtered:
                # Jump to one iteration before next day
                hour += ((23 - hour) // interval) * interval

            if byhour:
                ndays, hour = state._rrule__mod_distance(value=hour, byxxx=state._byhour, base=24)
            else:
                ndays, hour = divmod(hour + interval, 24)

            if ndays:
                day += ndays
                fixday = True

            timeset = gettimeset(hour, minute, second)
        elif freq == MINUTELY:
            if filtered:
                # Jump to one iteration before next day
                minute += ((1439 - (hour * 60 + minute)) // interval) * interval

            valid = False
            rep_rate = 24 * 60
            for _j in range(rep_rate // gcd(interval, rep_rate)):
                spend()
                if byminute:
                    nhours, minute = state._rrule__mod_distance(
                        value=minute, byxxx=state._byminute, base=60
                    )
                else:
                    nhours, minute = divmod(minute + interval, 60)

                div, hour = divmod(hour + nhours, 24)
                if div:
                    day += div
                    fixday = True
                    filtered = False

                if not byhour or hour in byhour:
                    valid = True
                    break

            if not valid:
                raise ValueError(
                    "Invalid combination of interval and " + "byhour resulting in empty rule."
                )

            timeset = gettimeset(hour, minute, second)
        elif freq == SECONDLY:
            if filtered:
                # Jump to one iteration before next day
                second += ((86399 - (hour * 3600 + minute * 60 + second)) // interval) * interval

            rep_rate = 24 * 3600
            valid = False
            for _j in range(rep_rate // gcd(interval, rep_rate)):
                spend()
                if bysecond:
                    nminutes, second = state._rrule__mod_distance(
                        value=second, byxxx=state._bysecond, base=60
                    )
                else:
                    nminutes, second = divmod(second + interval, 60)

                div, minute = divmod(minute + nminutes, 60)
                if div:
                    hour += div
                    div, hour = divmod(hour, 24)
                    if div:
                        day += div
                        fixday = True

                if (
                    (not byhour or hour in byhour)
                    and (not byminute or minute in byminute)
                    and (not bysecond or second in bysecond)
                ):
                    valid = True
                    break

            if not valid:
                raise ValueError(
                    "Invalid combination of interval, "
                    + "byhour and byminute resulting in empty"
                    + " rule."
                )

            timeset = gettimeset(hour, minute, second)

        if fixday and day > 28:
            daysinmonth = calendar.monthrange(year, month)[1]
            if day > daysinmonth:
                while day > daysinmonth:
                    spend()
                    day -= daysinmonth
                    month += 1
                    if month == 13:
                        month = 1
                        year += 1
                        if year > datetime.MAXYEAR:
                            state._len = total
                            return
                    daysinmonth = calendar.monthrange(year, month)[1]
                ii.rebuild(year, month)
