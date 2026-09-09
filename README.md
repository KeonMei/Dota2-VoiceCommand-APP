*English | [Русский](README.ru.md)*

# Dota2 Voice Command Assistant DRAFT

A Windows voice assistant that reacts to spoken Russian commands to:
- launch Steam, Dota 2, Chrome (opening Yandex Music) and Discord ("Basic minimum");
- drive Dota 2's client menus to queue a Ranked Roles match for a given role
  (e.g. "Start a ranked game as mid").

Speech recognition runs fully offline (Vosk). Clicking through Dota 2's own
in-game UI is done with computer vision (OpenCV template matching), since the
Panorama UI does not expose standard OS accessibility handles.

## 1. Tech stack

| Concern                          | Choice                        | Why |
|-----------------------------------|-------------------------------|-----|
| Speech recognition (RU, offline) | **Vosk** (`vosk-model-small-ru`) | Works fully offline with no API keys, low latency, ships a small ready-made Russian model. `faster-whisper` is more accurate but heavier on CPU/latency for continuous background listening — kept as a possible drop-in swap (see `src/dota_voice/speech.py`). |
| Audio input                      | `sounddevice`                 | Simple PCM stream, delivers `int16` frames that Vosk consumes directly. |
| Fuzzy command matching           | `rapidfuzz`                   | Fast C++ backend, tolerant to word reordering and small recognition errors. |
| Process / URI launching          | `subprocess`, `os.startfile`  | Standard OS facilities; `steam://rungameid/570` opens through the registered protocol handler. |
| Waiting for process/window ready | `psutil` + `pywin32` (`win32gui`) | Avoids clicking blind — waits for the process/window to actually appear, with a timeout. |
| Clicks / keyboard                | `pyautogui`                   | Simple click/hotkey API with a built-in fail-safe (mouse to a screen corner aborts). |
| Panorama UI element detection    | `OpenCV` (`matchTemplate`) + `mss` | More robust than hardcoded coordinates against small rendering differences; `mss` gives fast screen capture. |
| Configuration                    | `PyYAML`                      | Human-readable `config.yaml` / `commands.yaml`, editable without touching code. |
| Feedback                         | `pyttsx3` (offline TTS) + `winsound` | Spoken confirmation and a beep without any cloud TTS dependency. |
| Tray + hotkey                    | `pystray` + `keyboard`        | Tray icon with an on/off menu, plus a global hotkey for when continuous listening gets in the way. |

## 2. Architecture

```
main.py                         # entry point
src/dota_voice/
    config.py                   # loads config.yaml + commands.yaml
    logging_setup.py            # rotating file + console logging
    speech.py                   # SpeechListener: Vosk + sounddevice, background thread
    commands.py                 # CommandMatcher: fuzzy phrase -> command + params
    actions.py                  # ActionExecutor: runs a command's steps (self._handlers)
    vision.py                   # OpenCV template matching over an mss screen capture
    process_utils.py            # process launching, process/window readiness (psutil/win32gui)
    notify.py                   # TTS speech + beep feedback
    tray.py                     # tray icon, menu, global hotkey
    app.py                      # wires every module into one running application
tools/
    calibrate.py                # interactive UI template calibration tool
config/
    config.yaml                 # paths, timings, role synonyms, vision parameters
    commands.yaml               # declarative "command -> steps" definitions
templates/                      # reference PNG crops of Dota 2 UI elements (produced by calibration)
logs/                           # app.log (rotated)
```

Data flow:

```
Microphone --> SpeechListener (Vosk) --> text
   text --> CommandMatcher (rapidfuzz + roles) --> (command, params) | None
   (command, params) --> ActionExecutor.run_steps(steps, params)
        each step --> process_utils / vision+pyautogui / webbrowser / notify
```

Extensibility: adding a new voice command only means editing
`config/commands.yaml` (a new `phrases` + `steps` entry) — the core
(`actions.py`, `commands.py`) doesn't need to change as long as the new
command reuses existing step types. A brand new step **type** is added with a
single entry in `ActionExecutor._handlers`.

## 3. Sample configuration

Full files: [config/config.yaml](config/config.yaml), [config/commands.yaml](config/commands.yaml).

Role synonyms:

```yaml
roles:
  mid:
    label: "Mid"
    synonyms: ["мидер", "мид", "миддер", "middle", "вторая позиция", "2 позиция"]
```

Command steps (the "ranked game" example):

```yaml
- id: ranked_role
  phrases:
    - "начни рейтинговую игру на {role}"
    - "запусти рейтинг на {role}"
  role_param: role
  steps:
    - type: click_template
      template: play_button
      retries_key: max_retries
    - type: click_template
      template: ranked_roles_tab
    - type: select_exclusive_role   # clears any other selected role first
      retries_key: max_retries
    - type: click_template
      template: find_match_button
```

Voice commands themselves stay in Russian (the phrases in `commands.yaml`),
matching how the assistant is actually spoken to; only this documentation is
in English.

## 4. MVP implementation plan

**Stage 1 — "Basic minimum" (no image recognition needed):**
1. `process_utils.py` — launch an executable + wait for its process/window. ✅
2. `speech.py` + `commands.py` — recognize speech and fuzzy-match the "basic minimum" phrase. ✅
3. `actions.py` — `launch_process`, `launch_uri`, `open_url_in_chrome`, `sleep`, `notify` steps. ✅
4. `tray.py` — tray icon and the on/off hotkey. ✅

This stage is already a fully working MVP and can be tested without Dota 2
running at all.

**Stage 2 — ranked game through the Panorama UI (harder):**
1. `vision.py` — template matching over an `mss` screen capture. ✅
2. `tools/calibrate.py` — interactive template calibration (drag a rectangle over a screenshot). ✅
3. `actions.py`'s `click_template` step, with retries and a `click_point` fallback. ✅
4. The user calibrates 8 templates (see section 6 below) for their own resolution.
5. Test incrementally: start with `play_button` alone, then the full chain.

All of the above is already implemented in this repository (see the file
tree above) — this is working MVP code, not pseudocode.

## 5. Setup and running

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

> **If `pip install` fails with `ERROR: Failed to build 'PyYAML'`** (or any
> other package) **"when getting requirements to build wheel"** — this means
> no prebuilt wheel exists yet for your Python version at the exact pinned
> version, so pip falls back to building from source (which usually needs a
> C compiler that Windows doesn't have by default). `requirements.txt` pins
> versions as a lower bound (`>=`) rather than an exact match, so updating
> pip and retrying is usually enough:
> ```bash
> python -m pip install --upgrade pip
> pip install -r requirements.txt
> ```
> If one specific package still fails to build, try installing it
> unversioned (`pip install PyYAML`) so pip picks the latest release that
> ships a wheel for your Python.

Download the offline Russian speech model (Vosk):
1. Go to https://alphacephei.com/vosk/models
2. Download `vosk-model-small-ru-0.22` (fast, ~45 MB) or `vosk-model-ru-0.42` (more accurate, ~1.8 GB)
3. Unpack it so you end up with `models/vosk-model-small-ru-0.22/...` at the
   project root (or point `config/config.yaml -> speech.model_path` at a
   different location)

Review and adjust `config/config.yaml` for your machine:
- `apps.steam.path`, `apps.chrome.path`, `apps.discord.path` — the actual install paths on your machine;
- `dota2.app_id` — normally `570`, no need to change;
- `vision.calibrated_resolution` — the screen resolution the UI templates were
  calibrated for. Check/update it without running the whole app:

  ```bash
  python tools/calibrate.py --check-resolution
  ```

  It compares the current screen resolution against the one stored in the
  config and, if they differ (or nothing is set yet), updates `config.yaml`
  automatically. The same check runs automatically every time `main.py`
  starts — if the screen doesn't match the calibration, a warning is logged
  (`logs/app.log`) and printed to the console before you'd otherwise hit a
  confusing "UI element not found" while trying to start a game.

Run it:

```bash
python main.py
```

A tray icon appears (a green dot means listening is active). The
`Ctrl+Alt+L` hotkey (configurable at `config.yaml -> hotkeys.toggle_listening`)
temporarily pauses/resumes command recognition without killing the audio
stream.

> The global hotkey (via the `keyboard` library) sometimes needs the terminal
> running as Administrator on Windows — if it doesn't fire, try running
> `python main.py` from an elevated PowerShell.

## 6. Calibrating Dota 2 templates for your resolution

The "start a ranked game as ..." command relies on 13 templates. Calibrate
them once (and again whenever your screen resolution or the Dota 2 UI scale
changes). Check your current resolution first (see section 5 above):

```bash
python tools/calibrate.py --check-resolution
```

Template list:

| Template name              | What to capture                                                    |
|-----------------------------|--------------------------------------------------------------------|
| `play_button`               | The "Play" button in the Dota 2 main menu                          |
| `ranked_roles_tab`          | The "Ranked Roles" tab/entry on the game-mode selection screen     |
| `role_carry`                | Position 1 (Carry) icon, **unselected** state                      |
| `role_carry_selected`       | Position 1 (Carry) icon, **selected/highlighted** state            |
| `role_mid`                  | Position 2 (Mid) icon, unselected                                  |
| `role_mid_selected`         | Position 2 (Mid) icon, selected                                    |
| `role_offlane`              | Position 3 (Offlane) icon, unselected                              |
| `role_offlane_selected`     | Position 3 (Offlane) icon, selected                                |
| `role_support`              | Position 4 (Support) icon, unselected                              |
| `role_support_selected`     | Position 4 (Support) icon, selected                                |
| `role_hard_support`         | Position 5 (Hard Support) icon, unselected                         |
| `role_hard_support_selected`| Position 5 (Hard Support) icon, selected                           |
| `find_match_button`         | The button that confirms/starts the matchmaking search             |

**Why two states per role:** Dota's role icons are toggles, not an exclusive
choice — clicking one doesn't clear whatever was already selected from a
previous game. To fix that, `select_exclusive_role` (the step behind this
command, see `config/commands.yaml`) checks every role's `_selected`
template first; whichever roles show as selected but weren't the one you
asked for get clicked off, and only then is your requested role clicked on
(skipped if it's already selected). This needs a way to tell "selected" from
"not selected" apart on screen — hence the two templates per role.

For each template:

```bash
python tools/calibrate.py play_button
python tools/calibrate.py role_mid
python tools/calibrate.py role_mid_selected
```

1. The script gives you 5 seconds to switch to Dota 2 and open the relevant screen.
2. For a `_selected` template, click the role icon in-game first so it shows
   its selected/highlighted look, *then* run the calibration command for
   that state — the countdown gives you time to do this before the
   screenshot is taken.
3. It takes a fullscreen screenshot and opens it in a window.
4. Drag a rectangle over the element with the mouse (press and drag) — the
   tighter the crop around the button/icon, with as little background as
   possible, the more reliable the matching against different menu
   backgrounds. Crop the unselected and selected versions of a role
   identically (same bounds) so only the highlight differs between them.
5. Release the mouse button — the crop is saved to `templates/<name>.png`
   and the window closes automatically.

Repeat for all 13 names in the table. Then try the command:

```
"Начни рейтинговую игру на мидера"
```

...and try it again right after saying a *different* role (e.g. "...на
кэрри") to confirm the previous role gets cleared instead of stacking.

If an element isn't found (`vision.match_threshold` in `config.yaml`,
`0.86` by default), the log (`logs/app.log`) will show which template and
what score failed to match. Options:
- recalibrate the template more tightly (no background, exact element bounds);
- lower `match_threshold` slightly (e.g. to `0.8`);
- as a last resort, set `fallback_point: [x, y]` on that step in `commands.yaml`
  to use fixed coordinates (only supported by the plain `click_template` step,
  not `select_exclusive_role`).

## 7. Choosing a nicer TTS voice

By default the assistant speaks through whatever Russian SAPI5 voice Windows
has installed — usually "Microsoft Irina", which most people find harsh.
See what's actually installed on your machine:

```bash
python tools/list_voices.py
```

Then set it in `config/config.yaml`:

```yaml
feedback:
  tts_volume: 0.85     # 0.0-1.0, lowering this alone softens Irina noticeably
  tts_voice: "Zira"    # case-insensitive substring of a name from list_voices.py
```

`tts_voice: null` (the default) uses whatever voice Windows treats as
default. If the substring doesn't match any installed voice, a warning is
logged and the default voice is used instead — it never crashes the app.

If nothing installed sounds good, Windows 11 offers extra natural-sounding
voices under **Settings → Time & language → Speech → Manage voices → Add
voices**; install a Russian one there yourself (this repo won't touch system
settings for you), then rerun `list_voices.py` — if it doesn't show up, that
particular voice is likely a newer "OneCore" voice that classic SAPI5 apps
like this one can't see without extra manual Windows configuration, in which
case sticking with the tuned-down default voice is the simpler path for now.

## Error handling

- **Steam/Dota 2 already running** — `process_utils.launch_process` checks
  `process_name` via `psutil` and won't launch a second instance.
- **App not found at the configured path** — raises `ActionError` with a
  clear message, speaks "Не удалось выполнить шаг: ..." and stops the
  sequence instead of guessing what to do next.
- **UI template not found after N attempts** (`vision.max_retries`) —
  same handling: stop, log, speak, no blind clicking.

## Out of scope

The assistant never interacts with the game process once matchmaking starts
— no memory reading, no aimbots, no automation of in-game actions. All
automation is limited to the menus before the match search begins.
