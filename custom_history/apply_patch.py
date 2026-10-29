#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Patch the native AnkiDroid Reviewer before Gradle compiles the APK.

Copy on native Show Answer, save thinking/reflection/rating on native grading.
Only known Reviewer.kt anchors are changed; no generic JS-to-DB interface.
"""
from pathlib import Path
from zipfile import ZipFile


# The user-provided artwork is stored in one ZIP to avoid 18 manual GitHub uploads.
ICON_ARCHIVE = Path("custom_history/icon_resources.zip")
RESOURCE_DIR = Path("AnkiDroid/src/main/res")
DENSITIES = ("mdpi", "hdpi", "xhdpi", "xxhdpi", "xxxhdpi")
VALID_NAMES = {
    *(f"mipmap-{d}/{name}" for d in DENSITIES
      for name in ("ic_launcher.png", "ic_launcher_round.png", "ic_personal_foreground.png")),
    "mipmap-anydpi-v26/ic_launcher.xml",
    "mipmap-anydpi-v26/ic_launcher_round.xml",
    "values/personal_launcher_colors.xml",
}


def install_launcher_icons():
    if not ICON_ARCHIVE.exists():
        raise RuntimeError(
            "Missing custom_history/icon_resources.zip. "
            "Upload the icon_resources.zip file to your fork before building."
        )
    with ZipFile(ICON_ARCHIVE) as z:
        names = set(z.namelist())
        if names != VALID_NAMES:
            raise RuntimeError(
                f"Unexpected launcher resources: missing={sorted(VALID_NAMES-names)}, "
                f"extra={sorted(names-VALID_NAMES)}"
            )
        if z.testzip() is not None:
            raise RuntimeError("The personal icon ZIP is corrupted.")

        # Avoid Android resource duplicates if upstream uses WebP or XML in a
        # density folder while ours uses PNG for the same icon resource name.
        for d in DENSITIES:
            folder = RESOURCE_DIR / f"mipmap-{d}"
            if folder.is_dir():
                for existing in folder.iterdir():
                    if existing.is_file() and existing.stem in (
                        "ic_launcher", "ic_launcher_round", "ic_personal_foreground"
                    ):
                        existing.unlink()

        for name in sorted(VALID_NAMES):
            output = RESOURCE_DIR / name
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(z.read(name))


SOURCE = Path("AnkiDroid/src/main/java/com/ichi2/anki/Reviewer.kt")
MARK = "// PERSONAL_HISTORY_NATIVE_GRADE_V4_SHOW_ANSWER_COPY"
OLD_MARK_V3 = "// PERSONAL_HISTORY_NATIVE_GRADE_V3_RATING_ICON"
OLD_MARK = "// PERSONAL_HISTORY_NATIVE_GRADE_V1"
OLD_MARK_V2 = "// PERSONAL_HISTORY_NATIVE_GRADE_V2_NO_IDS"
KOTLIN_HELPERS = '\n    // PERSONAL_HISTORY_NATIVE_GRADE_V4_SHOW_ANSWER_COPY\n    // Keep recent attempt IDs in memory to prevent duplicate writes.\n    // These identifiers are NEVER saved into 答题历史.\n    private val personalHistorySavedIds = LinkedHashSet<String>()\n\n    private val personalHistoryNativeCopiedIds = LinkedHashSet<String>()\n\n\n    /** Copy only after the answer WebView has finished loading.\n     *  Android\'s ClipboardManager avoids JavaScript clipboard permission issues.\n     *  On failure, don\'t mark the attempt as copied.\n     */\n    private fun personalHistoryCopyOnAnswer(view: WebView) {\n        val expectedCardId = currentCard?.id ?: return\n        view.evaluateJavascript(\n            """\n            (function() {\n              try {\n                if (!document.getElementById("anki-back-side-flag")) return null;\n                var makePayload = window.AnkiStudyHistoryAutoGetCopyPayload;\n                if (typeof makePayload !== "function") return null;\n                var payload = makePayload();\n                return payload ? JSON.stringify(payload) : null;\n              } catch (e) {\n                return null;\n              }\n            })();\n            """.trimIndent(),\n        ) { raw ->\n            if (webView !== view || !isDisplayingAnswer || currentCard?.id != expectedCardId) {\n                return@evaluateJavascript\n            }\n            try {\n                // evaluateJavascript JSON-encodes the returned JavaScript string.\n                val encoded = org.json.JSONTokener(raw).nextValue() as? String\n                    ?: return@evaluateJavascript\n                val result = org.json.JSONObject(encoded)\n                val id = result.optString("entryId")\n                val thinking = result.optString("thinking")\n                val text = result.optString("text")\n                if (!Regex("[A-Za-z0-9_-]{6,100}").matches(id) ||\n                    thinking.isBlank() || thinking.length > 100_000 ||\n                    text.length > 400_000 ||\n                    !text.startsWith("【题目信息及解析】\\n\\n") ||\n                    !text.endsWith("\\n\\n【我的思路】\\n\\n" + thinking.trim()) ||\n                    id in personalHistoryNativeCopiedIds\n                ) {\n                    return@evaluateJavascript\n                }\n\n                val clipboard =\n                    getSystemService(android.content.Context.CLIPBOARD_SERVICE)\n                        as? android.content.ClipboardManager\n                        ?: return@evaluateJavascript\n                clipboard.setPrimaryClip(\n                    android.content.ClipData.newPlainText("Anki AI", text),\n                )\n                personalHistoryNativeCopiedIds.add(id)\n                if (personalHistoryNativeCopiedIds.size > 128) {\n                    personalHistoryNativeCopiedIds.remove(personalHistoryNativeCopiedIds.first())\n                }\n\n                // Ack is for the reflection UI; the in-memory ID also serves as\n                // proof of copy if the user grades before the JS ack completes.\n                view.evaluateJavascript(\n                    "(function() { var fn = window.AnkiStudyHistoryAutoConfirmCopied;" +\n                        " if (typeof fn === \'function\') fn(" +\n                        org.json.JSONObject.quote(id) + "); })();",\n                    null,\n                )\n            } catch (error: Exception) {\n                Timber.e(error, "Could not copy personal study history to clipboard")\n            }\n        }\n    }\n\n    private data class PersonalHistoryDraft(\n        val entryId: String,\n        val thinking: String,\n        val reflection: String,\n    )\n\n    /**\n     * Snapshot current card input at the native grading event.\n     * No generic JavaScript write permission is exposed.\n     */\n    private suspend fun readPersonalHistoryDraft(): PersonalHistoryDraft? {\n        val page = webView ?: return null\n        return try {\n            val raw =\n                kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.Main.immediate) {\n                    kotlinx.coroutines.withTimeoutOrNull(3000L) {\n                        suspendCancellableCoroutine<String> { continuation ->\n                            page.evaluateJavascript(\n                                """\n                                (function () {\n                                  try {\n                                    var state = JSON.parse(\n                                      sessionStorage.getItem("anki_study_history_auto_v1") || "null"\n                                    );\n                                    var answer = document.getElementById("my_answer");\n                                    var reflection = document.getElementById("my_reflection");\n                                    if (!state || !answer || !answer.value.trim()) return null;\n                                    return JSON.stringify({\n                                      entryId: state.entryId || "",\n                                      thinking: answer.value,\n                                      reflection: reflection ? reflection.value : "",\n                                      copied: state.copied === true\n                                    });\n                                  } catch (e) {\n                                    return null;\n                                  }\n                                })();\n                                """.trimIndent(),\n                            ) { result ->\n                                if (continuation.isActive) continuation.resume(result)\n                            }\n                        }\n                    }\n                } ?: return null\n\n            // WebView returns JSON-encoded JavaScript string values.\n            val encoded = org.json.JSONTokener(raw).nextValue() as? String ?: return null\n            val result = org.json.JSONObject(encoded)\n            val id = result.optString("entryId")\n            val thinking = result.optString("thinking")\n            val reflection = result.optString("reflection")\n            val copiedInTemplate = result.optBoolean("copied", false)\n            if (!copiedInTemplate && id !in personalHistoryNativeCopiedIds) return null\n            if (!Regex("[A-Za-z0-9_-]{6,100}").matches(id)) return null\n            if (thinking.isBlank() || thinking.length > 100_000 ||\n                reflection.length > 100_000) return null\n            PersonalHistoryDraft(id, thinking, reflection)\n        } catch (error: Exception) {\n            Timber.e(error, "Could not capture personal study history")\n            null\n        }\n    }\n\n    private fun personalHistoryEscape(value: String): String =\n        value\n            .replace("&", "&amp;")\n            .replace("<", "&lt;")\n            .replace(">", "&gt;")\n            .replace("\\"", "&quot;")\n            .replace("\'", "&#x27;")\n            .replace("\\r\\n", "\\n")\n            .replace("\\r", "\\n")\n            .replace("\\n", "<br>")\n\n\n    /**\n     * Append a clean, human-readable history entry. The attempt ID is\n     * deliberately not serialized into the Anki note.\n     */\n    private fun personalHistoryAppend(\n        oldHistory: String,\n        draft: PersonalHistoryDraft,\n        rating: Rating,\n    ): String {\n        val numberMatcher = Regex("""【第\\s*(\\d+)\\s*次】""")\n        val legacyNumberMatcher = Regex("""【作答记录\\s*#\\s*(\\d+)】""")\n        val number =\n            (numberMatcher.findAll(oldHistory).mapNotNull {\n                it.groupValues[1].toIntOrNull()\n            } + legacyNumberMatcher.findAll(oldHistory).mapNotNull {\n                it.groupValues[1].toIntOrNull()\n            }).maxOrNull()?.plus(1) ?: 1\n\n        val entry = buildString {\n            append("<b>【第")\n            append(number)\n            append("次】</b><br>评分：")\n            append(\n                when (rating) {\n                    Rating.AGAIN -> "Again"\n                    Rating.HARD -> "Hard"\n                    Rating.GOOD -> "Good"\n                    Rating.EASY -> "Easy"\n                    else -> "Unknown"\n                },\n            )\n            append("<br>思路：")\n            append(personalHistoryEscape(draft.thinking))\n            if (draft.reflection.isNotBlank()) {\n                append("<br>复盘：")\n                append(personalHistoryEscape(draft.reflection))\n            }\n        }\n        return if (oldHistory.isBlank()) entry else "$oldHistory<hr>$entry"\n    }\n'

REVIEW_ANCHOR = """        Timber.d("answerCardInner: $cardId $rating")
        var wasLeech = false
        undoableOp(this) {
            sched.answerCard(state, rating).also {"""

REVIEW_REPLACE = """        Timber.d("answerCardInner: $cardId $rating")
        // Snapshot current fields before the native grading changes the card.
        val personalHistoryDraft = readPersonalHistoryDraft()
        var wasLeech = false
        undoableOp(this) {
            if (
                personalHistoryDraft != null &&
                personalHistoryDraft.entryId !in personalHistorySavedIds
            ) {
                val note = getNote(state.topCard.nid)
                // Only update the dedicated field of the current note.
                if ("答题历史" in note) {
                    note.setItem(
                        "答题历史",
                        personalHistoryAppend(note.getItem("答题历史"), personalHistoryDraft, rating),
                    )
                    updateNote(note)
                    personalHistorySavedIds.add(personalHistoryDraft.entryId)
                    if (personalHistorySavedIds.size > 128) {
                        personalHistorySavedIds.remove(personalHistorySavedIds.first())
                    }
                }
            }
            sched.answerCard(state, rating).also {"""


# Fail closed if the upstream method changes; no generic JS write interface.
ANSWER_REVEAL_ANCHOR = '    override fun onPageFinished(view: WebView) {\n        super.onPageFinished(view)\n        onFlagChanged()\n        onMarkChanged()\n        if (!displayAnswer) {\n            runStateMutationHook()\n        }\n    }'
ANSWER_REVEAL_REPLACE = '    override fun onPageFinished(view: WebView) {\n        super.onPageFinished(view)\n        onFlagChanged()\n        onMarkChanged()\n        if (displayAnswer) {\n            personalHistoryCopyOnAnswer(view)\n        } else {\n            runStateMutationHook()\n        }\n    }'

def patch_text(source):
    if MARK in source:
        print("Native reviewer already contains the auto-copy upgrade.")
        return source
    if OLD_MARK in source or OLD_MARK_V2 in source or OLD_MARK_V3 in source:
        raise RuntimeError(
            "An earlier personal grading patch is already in Reviewer.kt. "
            "Run this script against the original upstream Reviewer.kt."
        )
    if source.count(ANSWER_REVEAL_ANCHOR) != 1:
        raise RuntimeError(
            "Reviewer.kt does not match the known onPageFinished anchor. "
            "Check the AnkiDroid source version before patching."
        )
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
                  .replace(ANSWER_REVEAL_ANCHOR, ANSWER_REVEAL_REPLACE, 1)
                  .replace(anchor, KOTLIN_HELPERS + '\n' + anchor, 1))

def main():
    if not SOURCE.is_file():
        raise RuntimeError("Run this from the Anki-Android repository root.")
    original = SOURCE.read_text(encoding="utf-8")
    updated = patch_text(original)
    SOURCE.write_text(updated, encoding="utf-8")
    print("Updated Reviewer.kt; copy on Show Answer, save rating/history on grading.")
    install_launcher_icons()
    print("Installed personal launcher icons.")

if __name__ == "__main__":
    main()
