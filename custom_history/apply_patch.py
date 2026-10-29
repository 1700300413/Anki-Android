#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Patch the native AnkiDroid Reviewer before Gradle compiles the APK.

V6: detect the Show Answer call without depending on neighboring code.\nIf needed, use WebView.onPageFinished with bounded retries.
Save thinking/reflection/rating on the existing native grading hook.
Only known Reviewer.kt anchors are changed; no generic JS-to-DB interface.
"""
from pathlib import Path
from zipfile import ZipFile
import re


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
MARK = "// PERSONAL_HISTORY_NATIVE_GRADE_V6_ADAPTIVE_ANSWER_HOOK"
OLD_MARK_V5 = "// PERSONAL_HISTORY_NATIVE_GRADE_V5_RELIABLE_ANSWER_COPY"
OLD_MARK_V4 = "// PERSONAL_HISTORY_NATIVE_GRADE_V4_SHOW_ANSWER_COPY"
OLD_MARK_V3 = "// PERSONAL_HISTORY_NATIVE_GRADE_V3_RATING_ICON"
OLD_MARK = "// PERSONAL_HISTORY_NATIVE_GRADE_V1"
OLD_MARK_V2 = "// PERSONAL_HISTORY_NATIVE_GRADE_V2_NO_IDS"
KOTLIN_HELPERS = '\n    // PERSONAL_HISTORY_NATIVE_GRADE_V6_ADAPTIVE_ANSWER_HOOK\n    // Keep recent attempt IDs in memory to prevent duplicate writes.\n    // These identifiers are NEVER saved into 答题历史.\n    private val personalHistorySavedIds = LinkedHashSet<String>()\n\n    private val personalHistoryNativeCopiedIds = LinkedHashSet<String>()\n\n\n\n    // A new native Show Answer action starts a new bounded copy request.\n    // This avoids depending on WebView.onPageFinished firing at the right time.\n    private var personalHistoryCopyRun = 0\n\n    private fun personalHistoryStartCopyAfterReveal() {\n        val expectedCardId = currentCard?.id ?: return\n        val request = ++personalHistoryCopyRun\n        Handler(Looper.getMainLooper()).postDelayed(\n            { personalHistoryTryCopyAfterReveal(expectedCardId, request, 0) },\n            120L,\n        )\n    }\n\n    private fun personalHistoryTryCopyAfterReveal(\n        expectedCardId: Long,\n        request: Int,\n        attempt: Int,\n    ) {\n        if (request != personalHistoryCopyRun ||\n            !isDisplayingAnswer ||\n            currentCard?.id != expectedCardId\n        ) {\n            return\n        }\n        val view = webView ?: return\n        fun retry(reason: String) {\n            if (attempt >= 24) {\n                Timber.w("Personal history auto-copy unavailable after retries: %s", reason)\n                return\n            }\n            Handler(Looper.getMainLooper()).postDelayed(\n                { personalHistoryTryCopyAfterReveal(expectedCardId, request, attempt + 1) },\n                160L,\n            )\n        }\n\n        // Probe the rendered answer and call only the template\'s restricted\n        // copy payload function. No generic JavaScript-to-native write API is added.\n        view.evaluateJavascript(\n            """\n            (function() {\n              try {\n                if (!document.getElementById("anki-back-side-flag")) {\n                  return JSON.stringify({status: "waiting_back"});\n                }\n                var answer = document.getElementById("my_answer");\n                if (!answer) return JSON.stringify({status: "waiting_thinking_box"});\n                if (!answer.value.trim()) return JSON.stringify({status: "empty"});\n                var getPayload = window.AnkiStudyHistoryAutoGetCopyPayload;\n                if (typeof getPayload !== "function") {\n                  return JSON.stringify({status: "waiting_template_js"});\n                }\n                var payload = getPayload();\n                if (!payload) return JSON.stringify({status: "waiting_payload"});\n                return JSON.stringify({status: "ready", payload: payload});\n              } catch (e) {\n                return JSON.stringify({status: "js_exception"});\n              }\n            })();\n            """.trimIndent(),\n        ) { raw ->\n            if (request != personalHistoryCopyRun ||\n                !isDisplayingAnswer ||\n                currentCard?.id != expectedCardId\n            ) {\n                return@evaluateJavascript\n            }\n            try {\n                val encoded = org.json.JSONTokener(raw).nextValue() as? String\n                if (encoded == null) {\n                    retry("WebView returned no usable string")\n                    return@evaluateJavascript\n                }\n                val envelope = org.json.JSONObject(encoded)\n                when (val status = envelope.optString("status")) {\n                    "empty" -> return@evaluateJavascript\n                    "ready" -> {\n                        val payload = envelope.optJSONObject("payload")\n                        if (payload == null) {\n                            retry("payload JSON missing")\n                            return@evaluateJavascript\n                        }\n                        val id = payload.optString("entryId")\n                        val thinking = payload.optString("thinking")\n                        val text = payload.optString("text")\n                        if (!Regex("[A-Za-z0-9_-]{6,100}").matches(id) ||\n                            thinking.isBlank() || thinking.length > 100_000 ||\n                            text.length > 400_000 ||\n                            !text.startsWith("【题目信息及解析】\\n\\n") ||\n                            !text.endsWith("\\n\\n【我的思路】\\n\\n" + thinking.trim())\n                        ) {\n                            Timber.w("Personal history payload format invalid")\n                            return@evaluateJavascript\n                        }\n                        if (id in personalHistoryNativeCopiedIds) {\n                            return@evaluateJavascript\n                        }\n                        val clipboard =\n                            getSystemService(android.content.Context.CLIPBOARD_SERVICE)\n                                as? android.content.ClipboardManager\n                        if (clipboard == null) {\n                            Timber.w("Personal history clipboard service unavailable")\n                            return@evaluateJavascript\n                        }\n                        clipboard.setPrimaryClip(\n                            android.content.ClipData.newPlainText("Anki AI", text),\n                        )\n                        // Only mark copied after the native clipboard write succeeds.\n                        personalHistoryNativeCopiedIds.add(id)\n                        if (personalHistoryNativeCopiedIds.size > 128) {\n                            personalHistoryNativeCopiedIds.remove(personalHistoryNativeCopiedIds.first())\n                        }\n                        view.evaluateJavascript(\n                            "(function() { var fn = window.AnkiStudyHistoryAutoConfirmCopied;" +\n                                " if (typeof fn === \'function\') fn(" +\n                                org.json.JSONObject.quote(id) + "); })();",\n                            null,\n                        )\n                        Timber.d("Personal history copied on Show Answer")\n                    }\n                    else -> retry(status)\n                }\n            } catch (error: Exception) {\n                Timber.e(error, "Personal history clipboard attempt failed")\n                retry("native_exception")\n            }\n        }\n    }\n\n    private data class PersonalHistoryDraft(\n        val entryId: String,\n        val thinking: String,\n        val reflection: String,\n    )\n\n    /**\n     * Snapshot current card input at the native grading event.\n     * No generic JavaScript write permission is exposed.\n     */\n    private suspend fun readPersonalHistoryDraft(): PersonalHistoryDraft? {\n        val page = webView ?: return null\n        return try {\n            val raw =\n                kotlinx.coroutines.withContext(kotlinx.coroutines.Dispatchers.Main.immediate) {\n                    kotlinx.coroutines.withTimeoutOrNull(3000L) {\n                        suspendCancellableCoroutine<String> { continuation ->\n                            page.evaluateJavascript(\n                                """\n                                (function () {\n                                  try {\n                                    var state = JSON.parse(\n                                      sessionStorage.getItem("anki_study_history_auto_v1") || "null"\n                                    );\n                                    var answer = document.getElementById("my_answer");\n                                    var reflection = document.getElementById("my_reflection");\n                                    if (!state || !answer || !answer.value.trim()) return null;\n                                    return JSON.stringify({\n                                      entryId: state.entryId || "",\n                                      thinking: answer.value,\n                                      reflection: reflection ? reflection.value : "",\n                                      copied: state.copied === true\n                                    });\n                                  } catch (e) {\n                                    return null;\n                                  }\n                                })();\n                                """.trimIndent(),\n                            ) { result ->\n                                if (continuation.isActive) continuation.resume(result)\n                            }\n                        }\n                    }\n                } ?: return null\n\n            // WebView returns JSON-encoded JavaScript string values.\n            val encoded = org.json.JSONTokener(raw).nextValue() as? String ?: return null\n            val result = org.json.JSONObject(encoded)\n            val id = result.optString("entryId")\n            val thinking = result.optString("thinking")\n            val reflection = result.optString("reflection")\n            val copiedInTemplate = result.optBoolean("copied", false)\n            if (!copiedInTemplate && id !in personalHistoryNativeCopiedIds) return null\n            if (!Regex("[A-Za-z0-9_-]{6,100}").matches(id)) return null\n            if (thinking.isBlank() || thinking.length > 100_000 ||\n                reflection.length > 100_000) return null\n            PersonalHistoryDraft(id, thinking, reflection)\n        } catch (error: Exception) {\n            Timber.e(error, "Could not capture personal study history")\n            null\n        }\n    }\n\n    private fun personalHistoryEscape(value: String): String =\n        value\n            .replace("&", "&amp;")\n            .replace("<", "&lt;")\n            .replace(">", "&gt;")\n            .replace("\\"", "&quot;")\n            .replace("\'", "&#x27;")\n            .replace("\\r\\n", "\\n")\n            .replace("\\r", "\\n")\n            .replace("\\n", "<br>")\n\n\n    /**\n     * Append a clean, human-readable history entry. The attempt ID is\n     * deliberately not serialized into the Anki note.\n     */\n    private fun personalHistoryAppend(\n        oldHistory: String,\n        draft: PersonalHistoryDraft,\n        rating: Rating,\n    ): String {\n        val numberMatcher = Regex("""【第\\s*(\\d+)\\s*次】""")\n        val legacyNumberMatcher = Regex("""【作答记录\\s*#\\s*(\\d+)】""")\n        val number =\n            (numberMatcher.findAll(oldHistory).mapNotNull {\n                it.groupValues[1].toIntOrNull()\n            } + legacyNumberMatcher.findAll(oldHistory).mapNotNull {\n                it.groupValues[1].toIntOrNull()\n            }).maxOrNull()?.plus(1) ?: 1\n\n        val entry = buildString {\n            append("<b>【第")\n            append(number)\n            append("次】</b><br>评分：")\n            append(\n                when (rating) {\n                    Rating.AGAIN -> "Again"\n                    Rating.HARD -> "Hard"\n                    Rating.GOOD -> "Good"\n                    Rating.EASY -> "Easy"\n                    else -> "Unknown"\n                },\n            )\n            append("<br>思路：")\n            append(personalHistoryEscape(draft.thinking))\n            if (draft.reflection.isNotBlank()) {\n                append("<br>复盘：")\n                append(personalHistoryEscape(draft.reflection))\n            }\n        }\n        return if (oldHistory.isBlank()) entry else "$oldHistory<hr>$entry"\n    }\n'

REVIEW_ANCHOR = """        Timber.d("answerCardInner: $cardId $rating")
        var wasLeech = false
        undoableOp(this) {
            sched.answerCard(state, rating).also {"""

REVIEW_REPLACE = """        Timber.d("answerCardInner: $cardId $rating")
        // Stop any unfinished clipboard retries when the native rating begins.
        personalHistoryCopyRun++
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


# V6: Match the actual reveal call without requiring unrelated methods
# to be adjacent in Reviewer.kt. Keep the patch fail-closed if it cannot
# identify a single unambiguous insertion point.
NATIVE_REVEAL_CALL = re.compile(
    r"(?m)^([ \t]*)super\.displayCardAnswer\(\)[ \t]*(?://[^\n]*)?$"
)
WEBVIEW_LOAD_CALL = re.compile(
    r"(?m)^([ \t]*)super\.onPageFinished\(view\)[ \t]*(?://[^\n]*)?$"
)


def _source_excerpt(source, phrase, radius=8):
    lines = source.splitlines()
    positions = [i for i, line in enumerate(lines) if phrase in line]
    if not positions:
        return f"No lines containing {phrase!r} found."
    parts = []
    for i in positions[:3]:
        lo = max(0, i-radius)
        hi = min(len(lines), i+radius+1)
        parts.append("\n".join(f"{j+1:5d}: {lines[j]}" for j in range(lo,hi)))
    return "\n---\n".join(parts)


def _install_show_answer_hook(source):
    native = list(NATIVE_REVEAL_CALL.finditer(source))
    if len(native) == 1:
        pos = native[0]
        original = pos.group()
        insertion = (
            original + "\n" + pos.group(1)
            + "// Start native clipboard copy after revealing the answer.\n"
            + pos.group(1) + "personalHistoryStartCopyAfterReveal()"
        )
        return source[:pos.start()] + insertion + source[pos.end():], "native"
    if len(native) > 1:
        raise RuntimeError(
            "Found more than one super.displayCardAnswer() call; refusing "
            "to patch ambiguously.\n" + _source_excerpt(source, "displayCardAnswer")
        )

    # Some older AnkiDroid source revisions handle the reveal without
    # calling super.displayCardAnswer() inside Reviewer.kt. In that case,
    # retry copying from a real WebView page-load callback.
    web = list(WEBVIEW_LOAD_CALL.finditer(source))
    if len(web) == 1 and re.search(r"\boverride\s+fun\s+onPageFinished\s*\(", source):
        pos = web[0]
        original = pos.group()
        insertion = (
            original + "\n" + pos.group(1)
            + "// Answer-side clipboard copy with delayed retry.\n"
            + pos.group(1) + "if (isDisplayingAnswer) personalHistoryStartCopyAfterReveal()"
        )
        return source[:pos.start()] + insertion + source[pos.end():], "onPageFinished"

    raise RuntimeError(
        "Could not safely locate Show Answer or WebView callback in Reviewer.kt.\n"
        "Relevant source context:\n"
        + _source_excerpt(source, "displayCardAnswer", 5)
        + "\n" + _source_excerpt(source, "onPageFinished", 5)
    )


def patch_text(source):
    if MARK in source:
        print("Reviewer.kt already contains the V6 patch.")
        return source
    older_markers = (OLD_MARK, OLD_MARK_V2, OLD_MARK_V3, OLD_MARK_V4, OLD_MARK_V5)
    if any(marker in source for marker in older_markers):
        raise RuntimeError(
            "Reviewer.kt already contains an older personal grading patch. "
            "Apply this script to a clean upstream Reviewer.kt, not an "
            "already patched file."
        )

    if source.count(REVIEW_ANCHOR) != 1:
        raise RuntimeError(
            "The native rating hook does not match Reviewer.kt.\n"
            "Relevant source context:\n"
            + _source_excerpt(source, "answerCardInner", 6)
        )
    helper_anchor = "    private suspend fun dealWithTimeBox("
    if source.count(helper_anchor) != 1:
        raise RuntimeError(
            "The Kotlin helper insertion point was not found uniquely.\n"
            "Relevant source context:\n"
            + _source_excerpt(source, "dealWithTimeBox", 4)
        )

    # Do not apply any changes until ALL hook anchors have been validated.
    source_with_reveal, hook_mode = _install_show_answer_hook(source)
    updated = (
        source_with_reveal.replace(REVIEW_ANCHOR, REVIEW_REPLACE, 1)
        .replace(helper_anchor, KOTLIN_HELPERS + "\n" + helper_anchor, 1)
    )
    assert updated.count(MARK) == 1
    assert updated.count("personalHistoryStartCopyAfterReveal()") >= 1
    print(f"V6: installed Show Answer clipboard hook via {hook_mode}.")
    return updated



# =============================================================================
# Personal new-reviewer patch (V7): UI, clipboard, grade history
# =============================================================================
FRAGMENT = Path('AnkiDroid/src/main/java/com/ichi2/anki/ui/windows/reviewer/ReviewerFragment.kt')
VIEWMODEL = Path('AnkiDroid/src/main/java/com/ichi2/anki/ui/windows/reviewer/ReviewerViewModel.kt')
LAYOUT = Path('AnkiDroid/src/main/res/layout/fragment_reviewer.xml')
NEW_MARK = '// PERSONAL_HISTORY_NEW_REVIEWER_V7'
NEW_LAYOUT_MARK = '<!-- PERSONAL_HISTORY_NEW_REVIEWER_V7 -->'


def _change_once(text, old, new, label):
    count=text.count(old)
    if count != 1:
        lines=text.splitlines()
        interesting=[i for i,line in enumerate(lines) if any(
            token in line for token in ('setupAnswerButtons','answerCardInternal',
                                        'showAnswerInternal','answer_area','tools_layout'))]
        context='\n'.join(f'{i+1}: {lines[i][:180]}' for i in interesting[:22])
        raise RuntimeError(f'New-reviewer anchor {label!r}: expected 1, found {count}.\n'
                           f'Check the fork source version.\nRelevant lines:\n{context}')
    return text.replace(old,new,1)


FRAGMENT_COPY_METHODS = r'''
    // PERSONAL_HISTORY_NEW_REVIEWER_V7
    // Read thinking before the new reviewer replaces the question HTML.
    // Read reflection just before the ViewModel performs the native rating.
    private var personalHistoryCopyJob: Job? = null

    private fun setupPersonalHistoryBridge() {
        viewModel.personalHistoryFrontRequests.collectIn(viewLifecycleOwner.lifecycleScope) { reply ->
            webViewLayout.evaluateJavascript(
                """
                (function () {
                    try {
                        if (document.getElementById("anki-back-side-flag")) return null;
                        var field = document.getElementById("my_answer");
                        return field ? field.value : null;
                    } catch (e) { return null; }
                })();
                """.trimIndent(),
            ) { raw -> if (!reply.isCompleted) reply.complete(raw) }
        }
        viewModel.personalHistoryReflectionRequests.collectIn(viewLifecycleOwner.lifecycleScope) { reply ->
            webViewLayout.evaluateJavascript(
                """
                (function () {
                    try {
                        if (!document.getElementById("anki-back-side-flag")) return null;
                        var field = document.getElementById("my_reflection");
                        return field ? field.value : "";
                    } catch (e) { return null; }
                })();
                """.trimIndent(),
            ) { raw -> if (!reply.isCompleted) reply.complete(raw) }
        }
    }

    private suspend fun personalHistoryEvaluate(script: String): String? =
        kotlinx.coroutines.withTimeoutOrNull(1200L) {
            kotlinx.coroutines.suspendCancellableCoroutine<String?> { continuation ->
                webViewLayout.evaluateJavascript(script) { raw ->
                    if (continuation.isActive) continuation.resume(raw)
                }
            }
        }

    private fun personalHistoryStartCopyOnAnswer() {
        personalHistoryCopyJob?.cancel()
        personalHistoryCopyJob =
            viewLifecycleOwner.lifecycleScope.launch {
                val cardId = viewModel.getCardId()
                val attempt = viewModel.personalHistoryCopyCandidate(cardId) ?: return@launch
                repeat(25) {
                    if (!viewModel.showingAnswer.value || viewModel.getCardId() != cardId) {
                        return@launch
                    }
                    // The new reviewer emits showingAnswer before _showAnswer() runs.
                    // Wait for the actual answer-side DOM, not merely a page load.
                    val raw = personalHistoryEvaluate(
                        """
                        (function () {
                            try {
                                if (!document.getElementById("anki-back-side-flag")) return null;
                                var template = document.getElementById("analysis-original");
                                if (!template || !template.content) return null;
                                var node = document.createElement("div");
                                node.appendChild(template.content.cloneNode(true));
                                node.querySelectorAll("img").forEach(function(img) { img.remove(); });
                                return node.innerHTML.trim();
                            } catch (e) { return null; }
                        })();
                        """.trimIndent(),
                    )
                    val analysis = try {
                        org.json.JSONTokener(raw ?: "null").nextValue() as? String
                    } catch (e: Exception) {
                        null
                    }
                    if (analysis != null && analysis.length <= 300_000) {
                        val text = "【题目信息及解析】\n\n" + analysis +
                            "\n\n【我的思路】\n\n" + attempt.thinking.trim()
                        try {
                            val clipboard = requireContext().getSystemService(android.content.Context.CLIPBOARD_SERVICE)
                                as? android.content.ClipboardManager
                            if (clipboard == null) {
                                Timber.w("Personal new reviewer: clipboard unavailable")
                                return@launch
                            }
                            clipboard.setPrimaryClip(android.content.ClipData.newPlainText("Anki AI", text))
                        } catch (error: Exception) {
                            Timber.e(error, "Personal new reviewer: clipboard write failed")
                            return@launch
                        }
                        if (!viewModel.personalHistoryMarkCopied(cardId, attempt.entryId)) return@launch
                        val thinking = org.json.JSONObject.quote(attempt.thinking)
                        webViewLayout.evaluateJavascript(
                            """
                            (function () {
                                try {
                                    var field = document.getElementById("my_answer");
                                    if (field && !field.value) field.value = $thinking;
                                    var area = document.getElementById("reflection-area");
                                    if (area) area.hidden = false;
                                    var state = JSON.parse(
                                        sessionStorage.getItem("anki_study_history_auto_v1") || "null"
                                    );
                                    if (state) {
                                        state.copied = true;
                                        state.thinking = $thinking;
                                        sessionStorage.setItem(
                                            "anki_study_history_auto_v1", JSON.stringify(state)
                                        );
                                    }
                                    if (typeof window.AnkiStudyHistoryAutoConfirmCopied === "function" &&
                                        state && state.entryId) {
                                        window.AnkiStudyHistoryAutoConfirmCopied(state.entryId);
                                    }
                                } catch (e) { console.warn(e); }
                            })();
                            """.trimIndent(),
                            null,
                        )
                        Timber.d("Personal new reviewer: answer copied")
                        return@launch
                    }
                    delay(160L)
                }
                Timber.w("Personal new reviewer: answer HTML unavailable after retries")
            }
    }

'''

VIEWMODEL_HELPERS = r'''
    // PERSONAL_HISTORY_NEW_REVIEWER_V7
    data class PersonalHistoryAttempt(
        val cardId: Long,
        val entryId: String,
        val thinking: String,
        var copied: Boolean = false,
    )

    val personalHistoryFrontRequests = MutableSharedFlow<CompletableDeferred<String?>>()
    val personalHistoryReflectionRequests = MutableSharedFlow<CompletableDeferred<String?>>()
    private var personalHistoryAttempt: PersonalHistoryAttempt? = null
    private val personalHistorySavedIds = LinkedHashSet<String>()

    fun personalHistoryCopyCandidate(cardId: Long): PersonalHistoryAttempt? =
        personalHistoryAttempt?.takeIf { it.cardId == cardId && !it.copied && it.thinking.isNotBlank() }

    fun personalHistoryMarkCopied(cardId: Long, entryId: String): Boolean {
        val attempt = personalHistoryAttempt ?: return false
        if (attempt.cardId != cardId || attempt.entryId != entryId || attempt.copied) return false
        attempt.copied = true
        return true
    }

    private fun personalHistoryDecodeJsString(raw: String?): String? =
        try {
            org.json.JSONTokener(raw ?: "null").nextValue() as? String
        } catch (error: Exception) {
            Timber.w(error, "Personal reviewer: invalid WebView data")
            null
        }

    private suspend fun personalHistoryCaptureBeforeFlip() {
        val cardId = currentCard.await().id
        val deferred = CompletableDeferred<String?>()
        val raw = withTimeoutOrNull(1400L) {
            personalHistoryFrontRequests.emit(deferred)
            deferred.await()
        }
        val thinking = personalHistoryDecodeJsString(raw)
        personalHistoryAttempt =
            if (thinking != null && thinking.isNotBlank() && thinking.length <= 100_000) {
                PersonalHistoryAttempt(cardId, java.util.UUID.randomUUID().toString(), thinking)
            } else {
                null
            }
    }

    private suspend fun personalHistoryGetReflection(): String {
        val deferred = CompletableDeferred<String?>()
        val raw = withTimeoutOrNull(1400L) {
            personalHistoryReflectionRequests.emit(deferred)
            deferred.await()
        }
        val reflection = personalHistoryDecodeJsString(raw)
        if (reflection == null || reflection.length > 100_000) {
            Timber.w("Personal reviewer: reflection unavailable, saving thought only")
            return ""
        }
        return reflection
    }

    private fun personalHistoryEscape(value: String): String =
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace("\"", "&quot;").replace("'", "&#x27;")
            .replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")

    private fun personalHistoryAppend(
        original: String,
        thought: String,
        reflection: String,
        rating: Rating,
    ): String {
        val nextRegex = Regex("""【第\s*(\d+)\s*次】""")
        val legacyRegex = Regex("""【作答记录\s*#\s*(\d+)】""")
        val next = (nextRegex.findAll(original).mapNotNull { it.groupValues[1].toIntOrNull() } +
            legacyRegex.findAll(original).mapNotNull { it.groupValues[1].toIntOrNull() })
            .maxOrNull()?.plus(1) ?: 1
        val label = when (rating) {
            Rating.AGAIN -> "Again"
            Rating.HARD -> "Hard"
            Rating.GOOD -> "Good"
            Rating.EASY -> "Easy"
            else -> "Unknown"
        }
        val record = "<b>【第${next}次】</b><br>评分：$label<br>思路：" +
            personalHistoryEscape(thought) +
            if (reflection.isNotBlank()) "<br>复盘：" + personalHistoryEscape(reflection) else ""
        return if (original.isBlank()) record else "$original<hr>$record"
    }

'''

def patch_new_fragment(source):
    if NEW_MARK in source:
        return source
    source=_change_once(source,
        'import android.content.Context\n',
        'import android.content.Context\nimport kotlin.coroutines.resume\n',
        'fragment-import')
    source=_change_once(source,
        '    private fun setupAnswerButtons() {',
        FRAGMENT_COPY_METHODS+'    private fun setupAnswerButtons() {',
        'fragment-insert-methods')
    source=_change_once(source,
        '        setupTypeAnswer()\n        setupAnswerButtons()',
        '        setupTypeAnswer()\n        setupPersonalHistoryBridge()\n        setupAnswerButtons()',
        'fragment-initialize-bridge')
    source=_change_once(source,
        '''        if (!Prefs.showAnswerButtons) {
            binding.answerArea.isVisible = false
            return
        }''',
        '''        binding.personalShowAnswerButton.setOnClickListener { viewModel.onShowAnswer() }
        if (!Prefs.showAnswerButtons) {
            binding.personalShowAnswerButton.isVisible = false
            binding.answerArea.isVisible = false
            return
        }''',
        'fragment-respect-show-buttons')
    source=_change_once(source,
        '''            binding.answerArea.setAnswerState(isAnswerShown)
        }''',
        '''            binding.answerArea.setAnswerState(isAnswerShown)
            binding.personalShowAnswerButton.isVisible = !isAnswerShown
            binding.answerArea.isVisible = isAnswerShown
            if (isAnswerShown) {
                personalHistoryStartCopyOnAnswer()
            } else {
                personalHistoryCopyJob?.cancel()
            }
        }''',
        'fragment-top-show-answer-flow')
    return source

def patch_new_viewmodel(source):
    if NEW_MARK in source:
        return source
    source=_change_once(source,
        '    private var isInputFocused = false\n',
        '    private var isInputFocused = false\n'+VIEWMODEL_HELPERS,
        'viewmodel-methods')
    source=_change_once(source,
        '''        updateNextTimes()
        showAnswer()
        loadAndPlayMedia(CardSide.ANSWER)''',
        '''        updateNextTimes()
        personalHistoryCaptureBeforeFlip()
        showAnswer()
        loadAndPlayMedia(CardSide.ANSWER)''',
        'viewmodel-capture-before-flip')
    source=_change_once(source,
        '''        val state = queueState.await() ?: return
        val card = currentCard.await()
        val answer =
            withCol {
                sched.answerCard(''',
        '''        val state = queueState.await() ?: return
        val card = currentCard.await()
        val historyAttempt = personalHistoryAttempt?.takeIf {
            it.cardId == card.id && it.copied && it.thinking.isNotBlank() &&
                it.entryId !in personalHistorySavedIds
        }
        val historyReflection = if (historyAttempt != null) personalHistoryGetReflection() else ""
        val answer =
            withCol {
                sched.answerCard(''',
        'viewmodel-grade-snapshot')
    source=_change_once(source,
        '        undoableOp(handler = this) { sched.answerCard(answer) }\n',
        '''        // Keep note history and native grading in one undoable transaction.
        var historyWasSaved = false
        undoableOp(handler = this) {
            if (historyAttempt != null) {
                val note = getNote(card.nid)
                if ("答题历史" in note) {
                    note.setItem(
                        "答题历史",
                        personalHistoryAppend(
                            note.getItem("答题历史"),
                            historyAttempt.thinking,
                            historyReflection,
                            rating,
                        ),
                    )
                    updateNote(note)
                    historyWasSaved = true
                }
            }
            sched.answerCard(answer)
        }
        if (historyWasSaved && historyAttempt != null) {
            personalHistorySavedIds.add(historyAttempt.entryId)
            if (personalHistorySavedIds.size > 128) {
                personalHistorySavedIds.remove(personalHistorySavedIds.first())
            }
        }
        personalHistoryAttempt = null
''',
        'viewmodel-grade-atomic-note')
    return source

PERSONAL_TOP_BUTTON = '''        <!-- PERSONAL_HISTORY_NEW_REVIEWER_V7 -->
        <com.google.android.material.button.MaterialButton
            android:id="@+id/personal_show_answer_button"
            style="@style/AnswerButton"
            android:layout_width="match_parent"
            android:layout_height="52dp"
            android:layout_marginHorizontal="@dimen/reviewer_side_margin"
            android:layout_marginVertical="4dp"
            android:text="@string/show_answer"
            android:textColor="@color/white"
            android:backgroundTint="?showAnswerButtonBackground"
            app:cornerRadius="12dp" />

'''

def patch_new_layout(source):
    if NEW_LAYOUT_MARK in source:
        return source
    return _change_once(source,
        '''        <com.google.android.material.card.MaterialCardView
            android:id="@+id/web_view_container"''',
        PERSONAL_TOP_BUTTON+
        '''        <com.google.android.material.card.MaterialCardView
            android:id="@+id/web_view_container"''',
        'layout-top-button')


def patch_new_reviewer_files():
    targets=(FRAGMENT,VIEWMODEL,LAYOUT)
    missing=[str(path) for path in targets if not path.is_file()]
    if missing:
        raise RuntimeError('Missing new study screen files: '+', '.join(missing))
    contents=[path.read_text(encoding='utf-8') for path in targets]
    outputs=(patch_new_fragment(contents[0]),
             patch_new_viewmodel(contents[1]),
             patch_new_layout(contents[2]))
    # Verify all upstream anchors before writing anything to disk.
    for path,changed in zip(targets,outputs):
        path.write_text(changed,encoding='utf-8')
    print('Patched new study screen: top Show Answer, clipboard, rating history.')


def main():
    # The new-screen files must be present. Fail closed if their version is new.
    for file in (FRAGMENT, VIEWMODEL, LAYOUT):
        if not file.is_file():
            raise RuntimeError(f'Missing new study screen source: {file}')
    # Apply new screen first; source matching is validated before any write.
    patch_new_reviewer_files()
    # Keep older study screen functionality when the fork matches one of the
    # known legacy variants. A mismatch here must not block the new reviewer.
    if SOURCE.is_file():
        old_source=SOURCE.read_text(encoding='utf-8')
        try:
            old_result=patch_text(old_source)
            SOURCE.write_text(old_result,encoding='utf-8')
            print('Legacy reviewer patch retained.')
        except RuntimeError as error:
            print('WARNING: old study screen patch not installed: '+str(error)[:800])
    install_launcher_icons()
    print('Installed personal launcher icons; new study screen patch ready.')

if __name__ == '__main__':
    main()
