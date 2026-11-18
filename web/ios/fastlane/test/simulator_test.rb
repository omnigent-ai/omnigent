require "json"
require "minitest/autorun"
require_relative "../simulator"

class OmnigentSimulatorTest < Minitest::Test
  def simctl(runtimes)
    devices = runtimes.to_h do |runtime, entries|
      ["com.apple.CoreSimulator.SimRuntime.#{runtime}",
       entries.map { |name, udid, state = "Shutdown"| { "name" => name, "udid" => udid, "state" => state } }]
    end
    JSON.dump("devices" => devices)
  end

  def test_picks_the_first_iphone_on_the_newest_ios_runtime
    json = simctl(
      "iOS-26-5" => [["iPhone 17 Pro", "OLD"]],
      "iOS-27-0" => [["iPad Pro", "PAD"], ["iPhone 18 Pro", "NEW"], ["iPhone Air", "AIR"]],
      "watchOS-12-0" => [["Apple Watch", "WATCH", "Booted"]]
    )
    assert_equal "NEW", OmnigentSimulator.default_iphone(json)["udid"]
  end

  def test_prefers_a_booted_iphone_on_any_runtime
    json = simctl(
      "iOS-26-5" => [["iPhone 17 Pro", "OLD", "Booted"]],
      "iOS-27-0" => [["iPhone 18 Pro", "NEW"]]
    )
    assert_equal "OLD", OmnigentSimulator.default_iphone(json)["udid"]
  end

  def test_compares_runtime_versions_numerically
    json = simctl("iOS-26-9" => [["iPhone 17", "NINE"]], "iOS-26-10" => [["iPhone 17", "TEN"]])
    assert_equal "TEN", OmnigentSimulator.default_iphone(json)["udid"]
  end

  def test_returns_nil_without_an_iphone
    assert_nil OmnigentSimulator.default_iphone(simctl("iOS-27-0" => [["iPad Pro", "PAD"]]))
    assert_nil OmnigentSimulator.default_iphone(JSON.dump("devices" => {}))
  end

  def test_requested_name_prefers_the_option_and_ignores_blanks
    assert_equal "iPhone Air", OmnigentSimulator.requested_name("iPhone Air", "iPhone 17")
    assert_equal "iPhone 17", OmnigentSimulator.requested_name("  ", "iPhone 17")
    assert_nil OmnigentSimulator.requested_name(nil, "")
  end
end
