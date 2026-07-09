package ai.omnigent.android

import android.util.Log

/**
 * Auth-flow trace, emitted only in debug builds. The login flow logs events
 * (never the token) for diagnosis, but even event-level auth traces shouldn't
 * ship in release logcat.
 */
fun authLog(message: String) {
    if (BuildConfig.DEBUG) Log.i("OmnigentAuth", message)
}
