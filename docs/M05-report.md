# M05: Dictation MVP (macOS), Report

Date: 2026-11-05 · Machine: MacBook Pro M4 Pro, macOS 27.2 · whisper.cpp 1.9.5 (Metal), model `small` · built-in mic and speakers
Status: **implemented and tested on macOS. Owner review requested before M06.**

## Summary

- **Dictation works end to end:** click the mic, speak, Stop, and the text appears in the app you were using.
  - Acoustic test (macOS `say` through the speakers into the microphone, 12 dictations in English, Indonesian and mixed): **12/12 first words kept**, 0 % word errors in English and Indonesian, 8 % in mixed speech, all from the Indonesian TTS voice mispronouncing "Docker"/"deploy".
  - **Stop → text inserted: 0.66 s median** for 5-second recordings.
- **Insertion verified in real apps:**
  - TextEdit (direct Accessibility insertion, 30–60 ms), Safari (web textarea), Terminal, and a Qt app: all correct, including emoji, `—`, accents and multi-line text.
  - iTerm received the text, but the read-back was inconclusive.
  - Your clipboard, including non-text content, is restored after pasting.
- **Text is never lost:**
  - It is saved to History *before* insertion.
  - Any insertion failure leaves it on the widget for Copy.
  - A transcription failure keeps the audio in memory for Retry until dismissed.
- **Safety guards, verified with real apps:**
  - OmonFlow never types into password fields, never types while Secure Input is on, and refuses to insert unless the target app is the live frontmost app at that exact moment.
  - Cancel and "insertion started" are decided atomically, so a cancelled dictation can never type anything.

## Incident during testing (please check)

Before the frontmost-app guard existed, one test ran after its target app had quit. macOS's cached "frontmost app" was stale, so OmonFlow pasted the test text **"tidak ada"** into whatever app was really in front (likely your terminal or Firefox/Chrome). It was a short string with no Return, so nothing was submitted. The bug is fixed and covered by two tests with a bystander app that must stay empty.

## What was built

| Area | Delivered | Location |
|---|---|---|
| Insertion interface | `Target`, `InsertResult` (inserted / copied / failed / not_started, method, verified), strategies auto / paste / type, clipboard-only fallback for platforms without an adapter | `insertion/base.py` |
| macOS adapter | Live frontmost app (Accessibility → LaunchServices → window server as last resort); target + focused element captured at the mic click; focus restore (`activate`, then `AXFrontmost` + `AXRaise` for macOS 14+ cooperative activation); **guards**: password field (subrole *or* role description), Secure Input, target quit, target not frontmost right before injection. **Layers**: (1) Accessibility `AXSelectedText` with read-back verification, (2) Unicode typing (single line only), (3) clipboard + ⌘V with full clipboard save/restore and transient marking. Read-back verification after paste/typing where the app exposes its value. Without Accessibility permission: copy + "press ⌘V" | `platform/macos/insert.py` |
| Focus tracker | Last non-OmonFlow app, from workspace activation notifications (one observer class per process) | `platform/macos/insert.py` |
| Orchestrator | Real transcription (timeout max(30 s, 3× duration)) → insertion. "No speech" handling: empty text, whisper markers, or a known silence phrase when no speech activity was detected. History written before insertion and updated with the outcome. Retry transcription with kept audio. Atomic cancel vs insertion start | `app/orchestrator.py` |
| History | SQLite in the user data folder: text, app, result, duration, model. Search (wildcards escaped), copy, delete one, delete all (+ VACUUM). No audio | `storage/history.py`, `ui/history.py` |
| Settings | Sidebar layout (8 sections). New: **Dictation** (how to dictate, language Auto/English/Indonesian, bilingual vocabulary hint), **Text Insertion** (permission status + "Ask macOS" + Privacy Settings, method, restore clipboard, last insertion diagnostics), **Privacy & History** (history on/off, open, delete all, what is stored/sent) | `ui/settings/window.py` |
| Tray | History… enabled | `ui/tray/tray.py` |
| Tests | Full dictation flow with fakes (all success/failure/cancel paths), history, real macOS insertion into a test app (12 tests incl. bystander safety) | `tests/test_dictation_flow.py`, `test_history.py`, `test_macos_insert_hw.py`, `insert_target.py` |
| E2E tools | Real-app insertion matrix; acoustic spoken corpus | `tools/e2e/insertion_matrix.py`, `tools/e2e/spoken_corpus.py` |

## Acceptance criteria (PLAN.md M05)

| Criterion | Result | Evidence |
|---|---|---|
| End-to-end dictation into the insertion matrix apps, results per app and layer | **Partly** | `docs/m05/insertion_matrix.json`: TextEdit 6/6, Safari 6/6, Terminal 3/3 correct; iTerm 3/3 inserted but unconfirmed (iTerm drops the trailing Return the test needed). Qt test app 12/12. Full acoustic dictation into a test app 12/12 + via the real app wiring. **Not tested:** Chrome, Firefox, WhatsApp (running, left alone); Slack, Word, Obsidian (need sign-in or setup); RDP/VNC clients (none installed) |
| Every cancel row of SPEC §5.3 | **Pass** | `test_dictation_flow.py`: cancel in capture (M03 tests), transcription, before insertion (never types), during insertion (refused); refinement row arrives with M07 |
| No transcript lost on any failure path | **Pass** | Insertion failure → Copy + History "failed"; no permission → copied + History "copied"; transcription failure → Retry with kept audio |
| Clipboard restored | **Pass** | Real test: text + custom data type restored after paste |
| Indonesian/English first-word check | **Pass (synthetic voices)** | `docs/m05/spoken_corpus.json`: 12/12 first words, Samantha (EN) + Damayanti (ID), through speaker → mic |
| Ad-hoc packaged app tested with real permission prompts | **Not done** | Packaged app builds, signs and starts; the Microphone and Accessibility prompts need you (steps below) |

## Measurements (M4 Pro, `small`, Metal)

| Metric | Value |
|---|---|
| Mic click → "Speak now" | 77–100 ms (297 ms on the very first start) |
| Stop → text inserted, median of 12 (5 s recordings) | **660 ms** (636–786) |
| Insertion: Accessibility (TextEdit) | 30–56 ms |
| Insertion: typing | ~70 ms |
| Insertion: paste incl. clipboard restore | ~310–350 ms |
| Word error rate: English / Indonesian / mixed | 0 % / 0 % / 8.3 % (TTS mispronunciations) |

## Defects found and fixed in M05

| Defect | Fix |
|---|---|
| **Stale frontmost app → text pasted into the wrong app** (incident above) | Live frontmost from Accessibility/LaunchServices; liveness check; final guard right before injection |
| Firefox doesn't answer system-wide Accessibility focus queries (`-25204`) | LaunchServices fallback; window list only as last resort (wrong on multi-display) |
| Focus could not be restored under macOS 14+ cooperative activation | `AXFrontmost` + `AXRaise` |
| Qt password fields not recognised as secure | Also check the role description; re-check at insertion time |
| A cancelled insertion could still type in a tiny window | Atomic check-and-claim under one lock |
| Second inserter lost focus tracking (Objective-C class redefinition) | One observer class per process |
| Eight settings tabs truncated | Sidebar navigation |

## Known limitations and open items

1. **Needs you, with the packaged app** (`dist/OmonFlow.app`, ad-hoc signed):
   1. Open it.
   2. Click the mic: macOS should ask for **Microphone** access for "OmonFlow".
   3. Dictate into an app: without **Accessibility** access the text is copied with a "press ⌘V" message.
   4. Grant access via Settings → Text Insertion → *Ask macOS for access…* and try again.
   5. Remember: ad-hoc builds may ask again after each rebuild.
2. **Real voices not yet tested.** The corpus uses synthetic voices. Speech-activity thresholds (RECORDING state) are unchanged from M03.
3. **Untested apps:** Chrome/Firefox (Electron-like web views behave like Safari's textarea in principle, unverified), Slack/WhatsApp (Electron), Word, terminals other than Terminal, remote-desktop clients.
4. **"Inserted" is sometimes unverifiable.** Apps that don't expose their text to Accessibility (many terminals, some web views) can't be read back; OmonFlow then reports inserted without verification. The text is always in History.
5. **The ⌘⌥ opt-in modifier-only shortcut is still deferred.** It needs keyboard monitoring and the Input Monitoring permission; proposed for M08 together with the Windows/Linux keyboard hooks.
6. **Refinement (Clean/AI) arrives in M07.** Dictation inserts Whisper's raw text.

## How to reproduce

```bash
uv run pytest -q                                                      # 163 tests
OMONFLOW_HW_TESTS=1 uv run pytest -q tests/test_macos_insert_hw.py    # real insertion (needs Accessibility)
uv run python tools/e2e/insertion_matrix.py                           # TextEdit/Safari/Terminal/iTerm (only if not running)
uv run python tools/e2e/spoken_corpus.py --rounds 2                   # plays speech aloud
```
