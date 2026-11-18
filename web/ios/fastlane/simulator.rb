require "json"

# Picks the simulator for the `simulator` lane from `xcrun simctl list devices available --json`.
module OmnigentSimulator
  module_function

  # An explicit `device:` option wins over OMNIGENT_IOS_SIMULATOR; blanks mean unset.
  def requested_name(option, env)
    [option, env].map { |value| value.to_s.strip }.find { |value| !value.empty? }
  end

  # Booted iPhone first, else the first iPhone on the newest iOS runtime; nil if none.
  def default_iphone(simctl_json)
    runtimes = JSON.parse(simctl_json).fetch("devices").select { |runtime, _| runtime.include?(".SimRuntime.iOS-") }
    runtimes = runtimes.sort_by { |runtime, _| runtime[/iOS-([\d-]+)\z/, 1].to_s.split("-").map(&:to_i) }.reverse
    iphones = runtimes.flat_map { |_, devices| devices.select { |device| device["name"].start_with?("iPhone") } }
    iphones.find { |device| device["state"] == "Booted" } || iphones.first
  end
end
