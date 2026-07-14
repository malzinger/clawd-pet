"""Language / i18n — DE/EN strings, number formats, tool phrases."""
from typing import Optional

_LANG = "de"

TOOL_BUBBLES = {
    "de": {
        "Read": "liest Dateien …",
        "Edit": "schreibt Code …",
        "Write": "schreibt Code …",
        "MultiEdit": "schreibt Code …",
        "NotebookEdit": "schreibt Code …",
        "Bash": "führt Befehle aus …",
        "PowerShell": "führt Befehle aus …",
        "Grep": "durchsucht den Code …",
        "Glob": "durchsucht den Code …",
        "Task": "delegiert an Agenten …",
        "Agent": "delegiert an Agenten …",
        "WebFetch": "surft im Web …",
        "WebSearch": "surft im Web …",
    },
    "en": {
        "Read": "reading files …",
        "Edit": "writing code …",
        "Write": "writing code …",
        "MultiEdit": "writing code …",
        "NotebookEdit": "writing code …",
        "Bash": "running commands …",
        "PowerShell": "running commands …",
        "Grep": "searching the code …",
        "Glob": "searching the code …",
        "Task": "delegating to agents …",
        "Agent": "delegating to agents …",
        "WebFetch": "browsing the web …",
        "WebSearch": "browsing the web …",
    },
}


def tool_bubble(tool) -> Optional[str]:
    return TOOL_BUBBLES.get(_LANG, TOOL_BUBBLES["de"]).get(tool or "")


# Detailed phrasing that names the concrete target ("{d}"), used by the panel's
# "what Clawd is working on" line and by the enriched speech bubbles.
TOOL_ACTIONS = {
    "de": {
        "Read": "liest {d}", "Edit": "bearbeitet {d}", "Write": "schreibt {d}",
        "MultiEdit": "bearbeitet {d}", "NotebookEdit": "bearbeitet {d}",
        "Bash": "führt aus: {d}", "PowerShell": "führt aus: {d}",
        "Grep": "durchsucht: {d}", "Glob": "durchsucht: {d}",
        "Task": "delegiert: {d}", "Agent": "delegiert: {d}",
        "WebFetch": "surft: {d}", "WebSearch": "sucht: {d}",
    },
    "en": {
        "Read": "reading {d}", "Edit": "editing {d}", "Write": "writing {d}",
        "MultiEdit": "editing {d}", "NotebookEdit": "editing {d}",
        "Bash": "running: {d}", "PowerShell": "running: {d}",
        "Grep": "searching: {d}", "Glob": "searching: {d}",
        "Task": "delegating: {d}", "Agent": "delegating: {d}",
        "WebFetch": "browsing: {d}", "WebSearch": "searching: {d}",
    },
}


def tool_action(tool, detail: str) -> str:
    """Localized phrase naming the concrete target, e.g. 'bearbeitet foo.py'."""
    if not detail:
        return tool_bubble(tool) or ""
    tmpl = TOOL_ACTIONS.get(_LANG, TOOL_ACTIONS["de"]).get(tool or "")
    return tmpl.format(d=detail) if tmpl else (tool_bubble(tool) or detail)
STRINGS = {
    "de": {
        "panel_title": "Plan-Nutzungslimits · {plan}",
        "row_5h": "5-Stunden-Limit",
        "row_week_all": "Wöchentlich · alle Modelle",
        "row_week_model": "Wöchentlich · {name}",
        "tokens_n": "{n} Tokens",
        "tokens_inout": "{n} Tokens (In + Out)",
        "rolling7": "≈ letzte 7 Tage",
        "reset_running": "Zurücksetzung läuft …",
        "reset_in_hm": "Zurücksetzung in {h} Std. {m:02d} Min.",
        "reset_in_m": "Zurücksetzung in {m} Min.",
        "reset_at": "Zurücksetzung {wd}, {t}",
        "weekdays": ("Mo.", "Di.", "Mi.", "Do.", "Fr.", "Sa.", "So."),
        "detail_used": "{n} Tokens verbraucht (Input + Output) · {hint}",
        "hint_manual": "Limit kalibriert",
        "hint_auto": "Limits automatisch kalibriert",
        "hint_placeholder": "Platzhalter-Limit – im Tray-Menü kalibrieren",
        "detail_local": "Lokal gezählt (5-h-Fenster): {parts} Tokens",
        "updated": "Zuletzt aktualisiert: {t} ({src})",
        "src_live": "live",
        "src_local": "lokal",
        "tooltip_wait": "Clawd wartet auf die ersten Daten …",
        "tooltip_api": "Claude-Nutzung (5-h-Fenster): {p}",
        "tooltip_est": "Claude-Nutzung (Schätzung): {p}  ({n} Tokens verbraucht)",
        "tray_title": "Clawd – Claude Code Nutzung",
        "tray_tooltip": "Clawd – {p}  ({n} Tokens verbraucht)",
        "bubble_done": "Fertig! Wartet auf dich.",
        "bubble_input": "Claude wartet auf deine Eingabe!",
        "bubble_session": "Neue Claude-Session gestartet",
        "menu_refresh": "Jetzt aktualisieren",
        "menu_panel": "Panel öffnen/schließen",
        "menu_quiet_on": "Sprechblasen einblenden",
        "menu_quiet_off": "Sprechblasen ausblenden",
        "menu_hooks_on": "Echtzeit-Hooks aktivieren (Beta)",
        "menu_hooks_off": "Echtzeit-Hooks deaktivieren",
        "menu_cal": "Limit kalibrieren …",
        "menu_cal_reset": "Kalibrierung zurücksetzen",
        "menu_lang": "Language: English",
        "menu_show": "Clawd anzeigen/verstecken",
        "menu_quit": "Beenden",
        "cal_api_title": "Kalibrierung nicht nötig",
        "cal_api_text": "Die App bezieht gerade die echten Prozentwerte direkt "
                        "von Anthropic. Eine Kalibrierung ändert daran nichts.",
        "cal_nodata_title": "Keine Daten",
        "cal_nodata_text": "Im aktuellen 5-Stunden-Fenster wurden keine Tokens "
                           "gezählt.\nNutze Claude Code kurz und versuche es erneut.",
        "cal_prompt_title": "Limit kalibrieren",
        "cal_prompt_text": "Öffne in Claude Code das Nutzungs-Popup (Befehl /usage).\n"
                           "Welchen Prozentwert zeigt dort das 5-Stunden-Limit?\n\n"
                           "Clawd hat im selben Fenster {n} Tokens gezählt.",
        "cal_done_title": "Kalibriert",
        "cal_done_text": "Dein 5-Stunden-Kontingent liegt bei etwa {n} Tokens.\n"
                         "Clawds Anzeige entspricht ab jetzt Claudes eigener.",
        "hooks_py_title": "Python benötigt",
        "hooks_py_text": "Für Echtzeit-Hooks wird eine Python-Installation benötigt\n"
                         "(pythonw/py im PATH). Der Log-Watcher läuft trotzdem weiter.",
        "hooks_on_title": "Hooks aktiviert",
        "hooks_on_text": "Clawd reagiert ab der nächsten Claude-Code-Session sofort\n"
                         "auf Ereignisse — inklusive „Claude wartet auf deine "
                         "Eingabe“.\n\nBackup der Einstellungen: {f}",
        "err_dir": "Log-Verzeichnis nicht gefunden: {p}",
        "err_logs": "Logs nicht lesbar: {e}",
        "err_scan": "Scan-Fehler: {e}",
        "menu_notify_on": "Benachrichtigungen aktivieren",
        "menu_notify_off": "Benachrichtigungen deaktivieren",
        "menu_autostart": "Beim Anmelden starten",
        "forecast_eta": "Bei diesem Tempo: Limit ca. {t} Uhr",
        "forecast_ok": "Tempo reicht bis zum Reset ✓",
        "notify_warn80_title": "Clawd wird nervös",
        "notify_warn80_text": "80 % des 5-Stunden-Limits sind verbraucht.",
        "notify_warn95_title": "Clawd ist in Panik!",
        "notify_warn95_text": "95 % verbraucht — gleich ist Schluss.",
        "notify_reset_title": "Budget wieder frisch!",
        "notify_reset_text": "Das 5-Stunden-Fenster wurde zurückgesetzt.",
        "single_title": "Clawd läuft bereits",
        "single_text": "Eine andere Clawd-Instanz läuft schon –\n"
                       "schau ins Tray oder auf deinen Desktop.",
        "history_title": "Verlauf (24 Std.)",
        "menu_check_updates": "Auf Updates prüfen",
        "menu_update": "⬇ Update {v} laden",
        "update_available": "Update {v} verfügbar!",
        "update_text": "Zum Herunterladen klicken.",
        "task_title": "Woran Clawd arbeitet",
        "task_project": "Projekt · {name}",
        "task_waiting": "Wartet auf dich",
        "task_quote": "„{s}“",
        "notify_done_title": "Claude ist fertig",
        "notify_done_text": "Dein Turn – Claude wartet auf dich.",
        "notify_input_title": "Claude braucht dich",
        "notify_input_text": "Claude wartet auf deine Eingabe.",
        "menu_sound_on": "Benachrichtigungston aktivieren",
        "menu_sound_off": "Benachrichtigungston deaktivieren",
        # --- cost estimate + projects (F4/F10) ---
        "cost_line": "≈ API-Gegenwert: {c5} (5 h) · {cw} (Woche)",
        "projects_line": "Projekte: {parts}",
        # --- motion (F5/F8) ---
        "menu_wander": "Herumlaufen",
        "menu_clickthrough": "Klicks durchlassen (neben Clawd)",
        # --- customization (F2/F13) ---
        "menu_size": "Größe",
        "menu_sprites_choose": "Sprite-Ordner wählen …",
        "menu_sprites_reset": "Standard-Sprites verwenden",
        "sprites_invalid_title": "Kein Sprite-Pack",
        "sprites_invalid_text": "Der Ordner enthält keine bekannten "
                                "Clawd-GIFs\n(z. B. clawd-idle.gif).",
        # --- Clawd's own login (v1.8 upstream port) ---
        "menu_clawd_login": "Clawd-Login einrichten …",
        "clawd_login_title": "Clawd-Login",
        "clawd_login_prompt": "1. Im Browser einloggen und „Authorize“ klicken.\n"
                              "2. Den angezeigten Code (xxx#yyy) hier einfügen:",
        "clawd_login_ok": "Clawd-Login eingerichtet — Live-Werte sind aktiv.",
        "clawd_login_fail": "Login fehlgeschlagen: {e}",
        "clawd_login_nocode": "Kein Code eingegeben.",
        # --- sound test ---
        "menu_sound_test": "Sound testen",
        # --- permission bubble (F11) + DND ---
        "menu_perm_on": "Permission-Bubble aktivieren (Beta)",
        "menu_perm_off": "Permission-Bubble deaktivieren",
        "perm_on_title": "Permission-Bubble aktiviert",
        "perm_on_text": "Fragt Claude Code ab der nächsten Session nach einer "
                        "Berechtigung,\nerscheint eine Bubble am Pet: Erlauben/"
                        "Ablehnen per Klick.\nKeine Reaktion → der normale "
                        "Terminal-Prompt übernimmt.\n\nBackup der "
                        "Einstellungen: {f}",
        "perm_question": "Claude fragt: {tool} erlauben?",
        "perm_allow": "✓ Erlauben",
        "perm_deny": "✕ Ablehnen",
        "menu_dnd_on": "Nicht stören aktivieren",
        "menu_dnd_off": "Nicht stören deaktivieren",
        # --- live-status line (usage accuracy fixes) ---
        "src_uncalibrated": "lokal – unkalibriert, grobe Werte",
        "src_rate_limited": "Live-Sync pausiert bis {t} (Rate-Limit)",
        # --- X2: hook events + statusline ---
        "bubble_compact": "Ich räume den Kontext auf …",
        "menu_statusline_on": "Kontext-Anzeige aktivieren",
        "menu_statusline_off": "Kontext-Anzeige deaktivieren",
        "statusline_on_title": "Kontext-Anzeige aktiviert",
        "statusline_on_text": "Claude Code zeigt ab sofort Clawds Statuszeile mit "
                              "dem Kontext-Füllstand;\ndas Panel zeigt den Wert "
                              "live an.\n\nBackup der Einstellungen: {f}",
        "statusline_foreign_title": "Eigene Statuszeile erkannt",
        "statusline_foreign_text": "In den Claude-Code-Einstellungen ist bereits "
                                   "eine eigene Statuszeile konfiguriert.\nClawd "
                                   "überschreibt sie nicht — bitte zuerst manuell "
                                   "entfernen.",
        "row_context": "Kontext-Fenster",
    },
    "en": {
        "panel_title": "Plan usage limits · {plan}",
        "row_5h": "5-hour limit",
        "row_week_all": "Weekly · all models",
        "row_week_model": "Weekly · {name}",
        "tokens_n": "{n} tokens",
        "tokens_inout": "{n} tokens (in + out)",
        "rolling7": "≈ last 7 days",
        "reset_running": "Resetting …",
        "reset_in_hm": "Resets in {h} h {m:02d} min",
        "reset_in_m": "Resets in {m} min",
        "reset_at": "Resets {wd}, {t}",
        "weekdays": ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"),
        "detail_used": "{n} tokens used (input + output) · {hint}",
        "hint_manual": "Limit calibrated",
        "hint_auto": "Limits auto-calibrated",
        "hint_placeholder": "Placeholder limit – calibrate via the tray menu",
        "detail_local": "Locally counted (5 h window): {parts} tokens",
        "updated": "Last updated: {t} ({src})",
        "src_live": "live",
        "src_local": "local",
        "tooltip_wait": "Clawd is waiting for first data …",
        "tooltip_api": "Claude usage (5 h window): {p}",
        "tooltip_est": "Claude usage (estimate): {p}  ({n} tokens used)",
        "tray_title": "Clawd – Claude Code usage",
        "tray_tooltip": "Clawd – {p}  ({n} tokens used)",
        "bubble_done": "Done! Waiting for you.",
        "bubble_input": "Claude is waiting for your input!",
        "bubble_session": "New Claude session started",
        "menu_refresh": "Refresh now",
        "menu_panel": "Toggle panel",
        "menu_quiet_on": "Show speech bubbles",
        "menu_quiet_off": "Hide speech bubbles",
        "menu_hooks_on": "Enable real-time hooks (beta)",
        "menu_hooks_off": "Disable real-time hooks",
        "menu_cal": "Calibrate limit …",
        "menu_cal_reset": "Reset calibration",
        "menu_lang": "Sprache: Deutsch",
        "menu_show": "Show/hide Clawd",
        "menu_quit": "Quit",
        "cal_api_title": "No calibration needed",
        "cal_api_text": "The app is currently getting the exact percentages "
                        "straight from Anthropic. Calibration would not change that.",
        "cal_nodata_title": "No data",
        "cal_nodata_text": "No tokens were counted in the current 5-hour window.\n"
                           "Use Claude Code briefly and try again.",
        "cal_prompt_title": "Calibrate limit",
        "cal_prompt_text": "Open the usage popup in Claude Code (/usage command).\n"
                           "What percentage does the 5-hour limit show there?\n\n"
                           "Clawd counted {n} tokens in the same window.",
        "cal_done_title": "Calibrated",
        "cal_done_text": "Your 5-hour budget is roughly {n} tokens.\n"
                         "Clawd's display now matches Claude's own.",
        "hooks_py_title": "Python required",
        "hooks_py_text": "Real-time hooks need a Python installation\n"
                         "(pythonw/py on PATH). The log watcher keeps working anyway.",
        "hooks_on_title": "Hooks enabled",
        "hooks_on_text": "From the next Claude Code session on, Clawd reacts\n"
                         "instantly to events — including \"Claude is waiting for "
                         "your input\".\n\nSettings backup: {f}",
        "err_dir": "Log directory not found: {p}",
        "err_logs": "Logs unreadable: {e}",
        "err_scan": "Scan error: {e}",
        "menu_notify_on": "Enable notifications",
        "menu_notify_off": "Disable notifications",
        "menu_autostart": "Start at login",
        "forecast_eta": "At this pace: limit around {t}",
        "forecast_ok": "Current pace lasts until the reset ✓",
        "notify_warn80_title": "Clawd is getting nervous",
        "notify_warn80_text": "80 % of the 5-hour limit is used.",
        "notify_warn95_title": "Clawd is panicking!",
        "notify_warn95_text": "95 % used — almost out.",
        "notify_reset_title": "Fresh budget!",
        "notify_reset_text": "The 5-hour window has reset.",
        "single_title": "Clawd is already running",
        "single_text": "Another Clawd instance is already running –\n"
                       "check the tray or your desktop.",
        "history_title": "History (24 h)",
        "menu_check_updates": "Check for updates",
        "menu_update": "⬇ Get update {v}",
        "update_available": "Update {v} available!",
        "update_text": "Click to download.",
        "task_title": "What Clawd is working on",
        "task_project": "Project · {name}",
        "task_waiting": "Waiting for you",
        "task_quote": "“{s}”",
        "notify_done_title": "Claude is done",
        "notify_done_text": "Your turn — Claude is waiting for you.",
        "notify_input_title": "Claude needs you",
        "notify_input_text": "Claude is waiting for your input.",
        "menu_sound_on": "Enable notification sound",
        "menu_sound_off": "Disable notification sound",
        # --- cost estimate + projects (F4/F10) ---
        "cost_line": "≈ API equivalent: {c5} (5 h) · {cw} (week)",
        "projects_line": "Projects: {parts}",
        # --- motion (F5/F8) ---
        "menu_wander": "Wander around",
        "menu_clickthrough": "Click-through (around Clawd)",
        # --- customization (F2/F13) ---
        "menu_size": "Size",
        "menu_sprites_choose": "Choose sprite folder …",
        "menu_sprites_reset": "Use default sprites",
        "sprites_invalid_title": "No sprite pack",
        "sprites_invalid_text": "The folder contains no known "
                                "Clawd GIFs\n(e.g. clawd-idle.gif).",
        # --- Clawd's own login (v1.8 upstream port) ---
        "menu_clawd_login": "Set up Clawd login …",
        "clawd_login_title": "Clawd login",
        "clawd_login_prompt": "1. Sign in in the browser and click \"Authorize\".\n"
                              "2. Paste the code shown (xxx#yyy) here:",
        "clawd_login_ok": "Clawd login set up — live values are active.",
        "clawd_login_fail": "Login failed: {e}",
        "clawd_login_nocode": "No code entered.",
        # --- sound test ---
        "menu_sound_test": "Test sound",
        # --- permission bubble (F11) + DND ---
        "menu_perm_on": "Enable permission bubble (beta)",
        "menu_perm_off": "Disable permission bubble",
        "perm_on_title": "Permission bubble enabled",
        "perm_on_text": "From the next session on, when Claude Code asks for "
                        "a permission,\na bubble appears at the pet: "
                        "Allow/Deny with one click.\nNo reaction → the normal "
                        "terminal prompt takes over.\n\nSettings backup: {f}",
        "perm_question": "Claude asks: allow {tool}?",
        "perm_allow": "✓ Allow",
        "perm_deny": "✕ Deny",
        "menu_dnd_on": "Enable do not disturb",
        "menu_dnd_off": "Disable do not disturb",
        # --- live-status line (usage accuracy fixes) ---
        "src_uncalibrated": "local – uncalibrated, rough numbers",
        "src_rate_limited": "live sync paused until {t} (rate limited)",
        # --- X2: hook events + statusline ---
        "bubble_compact": "Compacting context …",
        "menu_statusline_on": "Enable context display",
        "menu_statusline_off": "Disable context display",
        "statusline_on_title": "Context display enabled",
        "statusline_on_text": "Claude Code now shows Clawd's status line with the "
                              "context-window fill;\nthe panel shows the value "
                              "live.\n\nSettings backup: {f}",
        "statusline_foreign_title": "Custom status line detected",
        "statusline_foreign_text": "Your Claude Code settings already contain a "
                                   "custom status line.\nClawd will not overwrite "
                                   "it — please remove it manually first.",
        "row_context": "Context window",
    },
}


def tr(key: str, **kw):
    table = STRINGS.get(_LANG) or STRINGS["de"]
    s = table.get(key, STRINGS["de"].get(key, key))
    if isinstance(s, tuple):
        return s
    return s.format(**kw) if kw else s


def set_language(lang: str) -> None:
    global _LANG
    _LANG = lang if lang in STRINGS else "de"


def language() -> str:
    return _LANG


def fmt_de(n: int) -> str:
    """Locale-aware thousands separator: 1.234.567 (de) / 1,234,567 (en)."""
    s = f"{n:,}"
    return s.replace(",", ".") if _LANG == "de" else s


def fmt_pct_de(pct: float) -> str:
    s = f"{pct:.1f}"
    return (s.replace(".", ",") if _LANG == "de" else s) + " %"


def _fmt_dur(seconds: float) -> str:
    """Compact duration: 5 -> '0:05', 74 -> '1:14', 3661 -> '1:01:01'."""
    s = max(0, int(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
