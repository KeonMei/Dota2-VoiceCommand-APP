*English | [Русский](README.ru.md)*

# Dota2 Voice Command Assistant

A Windows voice assistant that reacts to spoken Russian commands to:
- launch Steam, Dota 2, Chrome (opening Yandex Music) and Discord ("Basic minimum");
- drive Dota 2's client menus to queue a Ranked Roles match for a given role
  (e.g. "Start a ranked game as mid");
- queue a normal Turbo or All Pick match with only that mode ticked
  ("Запусти турбо", "Запусти олл пик");
- stop ("Стоп") whatever command is currently running.

Speech recognition runs fully offline (Vosk). Clicking through Dota 2's own
in-game UI is done with computer vision (OpenCV template matching), since the
Panorama UI does not expose standard OS accessibility handles.

## 1. Tech stack

| Concern                          | Choice                        | Why |
|-----------------------------------|-------------------------------|-----|
| Speech recognition (RU, offline) | **Vosk** (`vosk-model-small-ru`) | Works fully offline with no API keys, low latency, ships a small ready-made Russian model. |
| Audio input                      | `sounddevice`                 | Simple PCM stream, delivers `int16` frames that Vosk consumes directly. |
| Fuzzy command matching           | `rapidfuzz`                   | Fast C++ backend, tolerant to word reordering and small recognition errors. |
| Process / URI launching          | `subprocess`, `os.startfile`  | Standard OS facilities; `steam://rungameid/570` opens through the registered protocol handler. |
| Waiting for process/window ready | `psutil` + `pywin32` (`win32gui`) | Avoids clicking blind — waits for the process/window to actually appear, with a timeout. |
| Clicks / keyboard                | `pyautogui`                   | Simple click/hotkey API with a built-in fail-safe (mouse to a screen corner aborts). |
| Panorama UI element detection    | `OpenCV` (`matchTemplate`) + `mss` | More robust than hardcoded coordinates against small rendering differences. |
| Configuration                    | `PyYAML`                      | Human-readable `config.yaml` / `commands.yaml`, editable without touching code. |
| Feedback                         | `pyttsx3` (offline TTS) + `winsound` | Spoken confirmation without any cloud TTS dependency. |
| Tray + hotkey                    | `pystray` + `keyboard`        | Tray icon with an on/off menu, plus a global hotkey. |

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
    list_voices.py              # lists installed TTS voices
config/
    config.yaml                 # paths, timings, role synonyms, tunable parameters
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

One command is special: `stop` carries a `control: stop` field instead of
`steps` and is handled outside the normal busy-lock, so it can interrupt
whatever is currently running (`Application._handle_stop` /
`ActionExecutor.request_stop`) instead of waiting in line behind it.

## 3. Sample configuration

Full files: [config/config.yaml](config/config.yaml), [config/commands.yaml](config/commands.yaml)
— both are commented inline with every tunable option (matching thresholds,
retries, timeouts, TTS voice/volume, cursor movement smoothness, etc.), so
this README doesn't repeat them.

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

## 4. Setup and running

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

If `pip install` fails with `Failed to build ... when getting requirements
to build wheel` on a very new Python version, update pip and retry
(`requirements.txt` pins versions as a lower bound so this is usually
enough):

```bash
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Download the offline Russian speech model (Vosk):
1. Go to https://alphacephei.com/vosk/models
2. Download `vosk-model-small-ru-0.22` (fast, ~45 MB) or `vosk-model-ru-0.42` (more accurate, ~1.8 GB)
3. Unpack it so you end up with `models/vosk-model-small-ru-0.22/...` at the
   project root (or point `config/config.yaml -> speech.model_path` at a
   different location)

Review `config/config.yaml` for your machine — at minimum, the paths under
`apps.*` (Steam/Chrome/Discord) — and check the screen resolution templates
are calibrated for:

```bash
python tools/calibrate.py --check-resolution
```

Run it:

```bash
python main.py
```

Or create a desktop shortcut that starts it without a console window (run
this with the same Python/venv the requirements are installed in; re-run it
if you move the project folder):

```bash
python tools/create_shortcut.py
```

Only one instance runs at a time — opening the shortcut again just brings
the running assistant's window up. If startup fails, a message box shows the error and the
full traceback is saved to `logs/startup_error.log`.

A small window opens with one round button that turns listening on/off
(green and pulsing = listening). Closing the window keeps the assistant
running in the tray; click the tray icon to reopen it, or use its "Выход"
item to quit. A tray icon also appears (a green dot means listening is active). The
`Ctrl+Alt+L` hotkey (configurable at `config.yaml -> hotkeys.toggle_listening`)
temporarily pauses/resumes command recognition without killing the audio
stream. If the hotkey doesn't fire, try running `python main.py` from an
elevated PowerShell — the `keyboard` library sometimes needs that on Windows.

## 5. Calibrating Dota 2 templates for your resolution

The "start a ranked game as ..." command relies on 13 templates, calibrated
once (and again whenever your screen resolution or the Dota 2 UI scale
changes):

| Template name              | What to capture                                         |
|-----------------------------|---------------------------------------------------------|
| `play_button`               | The "Play" button in the Dota 2 main menu               |
| `ranked_roles_tab`          | The "Рейтинговая игра" header while that section is **open** |
| `ranked_roles_tab_inactive` | The same header while it's **closed** (normal-game section open) |
| `role_<id>` / `role_<id>_selected` | Each role icon in its unselected and selected/highlighted state — `<id>` is `carry`, `mid`, `offlane`, `support`, `hard_support` |
| `find_match_button`         | The button that confirms/starts the matchmaking search  |

A section header is clicked only when its closed look matches, so a
command never toggles a section that's already open. Two states per role are needed because Dota's role icons are toggles, not an
exclusive choice — clicking one doesn't clear whatever was already selected
from a previous game. The `select_exclusive_role` step tells "selected" from
"not selected" apart using these templates, clicks off anything stale, then
selects the role you asked for.

For each template:

```bash
python tools/calibrate.py play_button
python tools/calibrate.py role_mid
python tools/calibrate.py role_mid_selected
```

The script gives you 5 seconds to switch to Dota 2 and open the relevant
screen (for a `_selected` template, click the role icon first so it shows
its highlighted look), takes a screenshot, and lets you drag a tight
rectangle around the element — crop the unselected/selected pair identically
so only the highlight differs. Repeat for all 13 names, then test:

```
"Начни рейтинговую игру на мидера"
```

...and immediately again with a different role, to confirm the previous one
gets cleared instead of stacking.

### Normal game (Turbo / All Pick)

"Запусти турбо" / "Запусти олл пик" reuse `play_button` and
`find_match_button`, plus:

| Template name | What to capture |
|---------------|-----------------|
| `normal_game_tab` | The "Обычная игра" header while that section is **open** |
| `normal_game_tab_inactive` | The same header while it's **closed** (ranked section open) |
| `modes_show_all_collapsed` | The "Показать все режимы" line while collapsed (arrow + text) |
| `mode_<id>` / `mode_<id>_selected` | Each mode's row — checkbox **and** label together — unticked and ticked. `<id>` is `all_pick`, `turbo`, `single_draft`, `random_draft`, `ability_draft` (`config.yaml` → `modes`) |

Mode checkboxes are toggles just like role icons, and clicking the label
toggles the row too. The `select_exclusive_mode` step expands "Показать все
режимы" first (so a ticked hidden mode can't slip through), unticks every
other mode, ticks the target and re-checks until only it is on — so calibrate
all five modes, not only the ones you have commands for. Crop each pair
identically and keep the mouse off the list while capturing (a hovered row
looks different). Optionally, `python tools/calibrate.py --region mode_list`
over the expanded list speeds up the search.

Only `turbo` and `all_pick` have voice commands today; for another mode, copy
the `normal_all_pick` entry in `commands.yaml` and change its `id`, `phrases`
and `params` (`mode: single_draft`, etc.) — no code changes needed.

If an element isn't found, `logs/app.log` shows which template and score
failed to match — recalibrate it more tightly, lower `vision.match_threshold`
in `config.yaml`, or (for a plain `click_template` step only) set a
`fallback_point: [x, y]` on that step in `commands.yaml`.

## Error handling

- **Steam/Dota 2 already running** — won't launch a second instance.
- **App not found at the configured path, or a UI template not found after
  N attempts** — stops the sequence, logs it, and speaks a short error
  instead of guessing or clicking blind.

## Out of scope

The assistant never interacts with the game process once matchmaking starts
— no memory reading, no aimbots, no automation of in-game actions. All
automation is limited to the menus before the match search begins.
