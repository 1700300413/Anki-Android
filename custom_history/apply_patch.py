#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Patch the native AnkiDroid Reviewer before Gradle compiles the APK.

Only applies to the known Reviewer.kt grading anchors. Fails closed when upstream differs.
No generic JS -> database interface is added.
"""
from pathlib import Path

SOURCE = Path("AnkiDroid/src/main/java/com/ichi2/anki/Reviewer.kt")
MARK = "// PERSONAL_HISTORY_NATIVE_GRADE_V1"
KOTLIN_HELPERS = '\n    // PERSONAL_HISTORY_NATIVE_GRADE_V1\n    private data class PersonalHistoryDraft(\n        val entryId: String,\n        val thinking: String,\n        val reflection: String,\n    )\n\n    /**\n     * Snapshot current card input at the native grading event.\n     * No generic JavaScript write permission is exposed.\n     */\n    private suspend fun readPersonalHistoryDraft(): PersonalHistoryDraft? {\n        val page = webView ?: return null\n        return try {\n            val raw =\n                kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.Main.immediate) {\n                    kotlinx.coroutines.withTimeoutOrNull(3000L) {\n                        suspendCancellableCoroutine<String> { continuation ->\n                            page.evaluateJavascript(\n                                """\n                                (function () {\n                                  try {\n                                    var state = JSON.parse(\n                                      sessionStorage.getItem("anki_study_history_auto_v1") || "null"\n                                    );\n                                    var answer = document.getElementById("my_answer");\n                                    var reflection = document.getElementById("my_reflection");\n                                    if (!state || state.copied !== true || !answer ||\n                                        !answer.value.trim()) return null;\n                                    return JSON.stringify({\n                                      entryId: state.entryId || "",\n                                      thinking: answer.value,\n                                      reflection: reflection ? reflection.value : ""\n                                    });\n                                  } catch (e) {\n                                    return null;\n                                  }\n                                })();\n                                """.trimIndent(),\n                            ) { result ->\n                                if (continuation.isActive) continuation.resume(result)\n                            }\n                        }\n                    }\n                } ?: return null\n\n            // WebView returns JSON-encoded JavaScript string values.\n            val encoded = org.json.JSONTokener(raw).nextValue() as? String ?: return null\n            val result = org.json.JSONObject(encoded)\n            val id = result.optString("entryId")\n            val thinking = result.optString("thinking")\n            val reflection = result.optString("reflection")\n            if (!Regex("[A-Za-z0-9_-]{6,100}").matches(id)) return null\n            if (thinking.isBlank() || thinking.length > 100_000 ||\n                reflection.length > 100_000) return null\n            PersonalHistoryDraft(id, thinking, reflection)\n        } catch (error: Exception) {\n            Timber.e(error, "Could not capture personal study history")\n            null\n        }\n    }\n\n    private fun personalHistoryEscape(value: String): String =\n        value\n            .replace("&", "&amp;")\n            .replace("<", "&lt;")\n            .replace(">", "&gt;")\n            .replace("\\"", "&quot;")\n            .replace("\'", "&#x27;")\n            .replace("\\r\\n", "\\n")\n            .replace("\\r", "\\n")\n            .replace("\\n", "<br>")\n\n    private fun personalHistoryUpsert(\n        oldHistory: String,\n        draft: PersonalHistoryDraft,\n    ): String {\n        val entryMatcher =\n            Regex(\n                """<div\\s+data-anki-study-entry="([a-zA-Z0-9_-]+)"[^>]*>[\\s\\S]*?</div>""",\n            )\n        val numberMatcher = Regex("""【第\\s*(\\d+)\\s*次】""")\n        val legacyNumberMatcher = Regex("""【作答记录\\s*#\\s*(\\d+)】""")\n        val found = entryMatcher.findAll(oldHistory)\n            .firstOrNull { it.groupValues[1] == draft.entryId }\n\n        val number = if (found != null) {\n            numberMatcher.find(found.value)?.groupValues?.get(1)?.toIntOrNull()\n                ?: error("History entry exists but sequence number is missing")\n        } else {\n            (numberMatcher.findAll(oldHistory).mapNotNull {\n                it.groupValues[1].toIntOrNull()\n            } + legacyNumberMatcher.findAll(oldHistory).mapNotNull {\n                it.groupValues[1].toIntOrNull()\n            }).maxOrNull()?.plus(1) ?: 1\n        }\n\n        val entry = buildString {\n            append("<div data-anki-study-entry=\\"")\n            append(draft.entryId)\n            append("\\" data-anki-study-number=\\"")\n            append(number)\n            append("\\"><b>【第")\n            append(number)\n            append("次】</b><br>思路：")\n            append(personalHistoryEscape(draft.thinking))\n            if (draft.reflection.isNotBlank()) {\n                append("<br>复盘：")\n                append(personalHistoryEscape(draft.reflection))\n            }\n            append("</div>")\n        }\n        if (found != null) {\n            return oldHistory.replaceRange(found.range, entry)\n        }\n        return if (oldHistory.isBlank()) entry else "$oldHistory<hr>$entry"\n    }\n'

REVIEW_ANCHOR = """        Timber.d("answerCardInner: $cardId $rating")
        var wasLeech = false
        undoableOp(this) {
            sched.answerCard(state, rating).also {"""

REVIEW_REPLACE = """        Timber.d("answerCardInner: $cardId $rating")
        // Snapshot current fields before the native grading changes the card.
        val personalHistoryDraft = readPersonalHistoryDraft()
        var wasLeech = false
        undoableOp(this) {
            if (personalHistoryDraft != null) {
                val note = getNote(state.topCard.nid)
                // Write only the dedicated field of the currently reviewed note.
                if ("答题历史" in note) {
                    note.setItem(
                        "答题历史",
                        personalHistoryUpsert(note.getItem("答题历史"), personalHistoryDraft),
                    )
                    updateNote(note)
                }
            }
            sched.answerCard(state, rating).also {"""

def patch_text(source):
    if MARK in source:
        print("Native reviewer already patched.")
        return source
    if source.count(REVIEW_ANCHOR) != 1:
        raise RuntimeError(
            "Reviewer.kt does not match these source anchors. "
            "Please use the build log to identify the fork version; "
            "this script will not make an unverified insertion."
        )
    anchor = '    private suspend fun dealWithTimeBox('
    if source.count(anchor) != 1:
        raise RuntimeError("Could not identify a unique Kotlin helper insertion point.")
    return (source.replace(REVIEW_ANCHOR, REVIEW_REPLACE, 1)
                  .replace(anchor, KOTLIN_HELPERS + '\n' + anchor, 1))

def main():
    if not SOURCE.is_file():
        raise RuntimeError("Run this from the Anki-Android repository root.")
    original = SOURCE.read_text(encoding="utf-8")
    updated = patch_text(original)
    SOURCE.write_text(updated, encoding="utf-8")
    print("Updated Reviewer.kt; native answer buttons will write answer history.")

if __name__ == "__main__":
    main()
