import XCTest

@testable import Omnigent

final class WelcomeBlobBackgroundTests: XCTestCase {
  /// Time at which a blob's cycle reaches `progress`, undoing its phase offset.
  private func time(_ blob: WelcomeBlob, atProgress progress: Double) -> TimeInterval {
    blob.duration * (1 + progress) - blob.offset
  }

  func testCycleStartIsTheRestPose() {
    for blob in WelcomeBlob.all {
      let pose = blob.pose(at: time(blob, atProgress: 0))
      XCTAssertEqual(pose.dx, 0, accuracy: 0.001, blob.id)
      XCTAssertEqual(pose.dy, 0, accuracy: 0.001, blob.id)
      XCTAssertEqual(pose.scale, 1, accuracy: 0.001, blob.id)
    }
  }

  func testPoseHitsEachKeyframe() {
    for blob in WelcomeBlob.all {
      for keyframe in blob.keyframes.dropLast() {
        let pose = blob.pose(at: time(blob, atProgress: keyframe.at))
        XCTAssertEqual(pose.dx, keyframe.pose.dx, accuracy: 0.01, "\(blob.id) @ \(keyframe.at)")
        XCTAssertEqual(pose.dy, keyframe.pose.dy, accuracy: 0.01, "\(blob.id) @ \(keyframe.at)")
        XCTAssertEqual(
          pose.scale, keyframe.pose.scale, accuracy: 0.001, "\(blob.id) @ \(keyframe.at)")
      }
    }
  }

  /// The loop closes: just before a cycle ends the blob is back near rest, so
  /// wrapping into the next cycle does not jump.
  func testCycleEndApproachesRest() {
    for blob in WelcomeBlob.all {
      let pose = blob.pose(at: time(blob, atProgress: 0.9999))
      XCTAssertEqual(pose.dx, 0, accuracy: 0.05, blob.id)
      XCTAssertEqual(pose.dy, 0, accuracy: 0.05, blob.id)
      XCTAssertEqual(pose.scale, 1, accuracy: 0.001, blob.id)
    }
  }

  /// Reduce Motion renders time zero, which must still land inside the table.
  func testTimeZeroIsFinite() {
    for blob in WelcomeBlob.all {
      let pose = blob.pose(at: 0)
      XCTAssertTrue(pose.dx.isFinite && pose.dy.isFinite && pose.scale.isFinite, blob.id)
    }
  }
}
