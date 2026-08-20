package ai.omnigent.android

/**
 * JSON-encode a string so it can be interpolated safely into JavaScript passed
 * to `WebView.evaluateJavascript`. Escapes quotes, backslashes, newlines, `<`
 * (so a `</script>`-style payload can't break out of an inline context), and the
 * U+2028/U+2029 line terminators that are valid line breaks in a JS string but
 * not in JSON. Matched by code point so no invisible characters live in source.
 */
fun jsString(value: String): String =
    buildString {
        append('"')
        for (c in value) {
            when (c.code) {
                '"'.code -> append("\\\"")
                '\\'.code -> append("\\\\")
                '\n'.code -> append("\\n")
                '\r'.code -> append("\\r")
                '<'.code -> append("\\u003c")
                0x2028 -> append("\\u2028")
                0x2029 -> append("\\u2029")
                else -> append(c)
            }
        }
        append('"')
    }
