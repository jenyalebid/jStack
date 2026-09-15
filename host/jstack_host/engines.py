"""Which CLI — and which model — a fresh jRemote session runs.

Two facts live here, and they are different in kind:

* **The roster** — the engines we can spawn and the models each accepts. A
  constant, because it is a property of the CLIs installed on this Mac, not of
  anything the user chose. Served to the app at `GET /engines` so a new model is
  one edit HERE and reaches every device on the next fetch. The app must never
  carry its own copy: a hardcoded Swift list means an App Store build to add a
  model, and a stale one offers a model the CLI would refuse.

* **The per-agent defaults** — the user's choice of which engine an agent's new
  chat opens with, and which model each engine runs for that agent. A
  preference, so it is stored, keyed on the BASE agent (every seat of an agent
  shares it, same as the notification mutes) and falling back to the fleet
  default when unset.

Both model ids and both defaults were verified by live probe against the real
CLIs (`claude -p --model X`, `codex exec -m X`) rather than read off a docs
page — `gpt-5.6-pro` looks like a model, appears in the binary's own strings,
and is refused. Anything added below earns its row the same way.

**Resolution happens on the host, once.** `resolve()` is the only place a
missing engine or model becomes a concrete one, so every spawn path — the
app's new-chat menu, the share sheet, a pin, a desk-side spawn — honours the
agent's default without each caller re-implementing the fallback. A client
that names neither gets the agent's defaults; a client that names one gets it.

An unknown engine or model is a 400 at the router, never a silent downgrade to
the default. Running a different model than the caller asked for is the kind
of wrong that only surfaces much later, in a transcript nobody can explain.
"""

import json
import threading
from pathlib import Path
from . import hostenv

_STATE = hostenv.state_dir() / "jremote_engines.json"
_lock = threading.Lock()

# The fleet default: Claude, Opus 5. Today this is also what the CLI picks with
# no --model flag at all, so making it explicit changes nothing right now —
# it starts mattering the moment the account default moves under us, which is
# exactly the drift a named default exists to survive.
FLEET_ENGINE = "claude"

# Engines and their models. `default_model` is what an agent gets when it has
# no stored preference for that engine.
#
# `detail` is the one line the settings picker shows under a model's name. It
# says what the model is FOR, since that is the only basis on which anyone
# picks one; anything else (context size, price) belongs where it can be kept
# true.
ENGINES: list[dict] = [
    {
        "id": "claude",
        "name": "Claude Code",
        "default_model": "claude-opus-5",
        "models": [
            {"id": "claude-opus-5", "name": "Opus 5",
             "detail": "The default. Most capable for everyday work."},
            {"id": "claude-sonnet-5", "name": "Sonnet 5",
             "detail": "Faster and cheaper; strong on well-scoped work."},
            {"id": "claude-fable-5", "name": "Fable 5",
             "detail": "Most capable for the hardest, longest-running tasks."},
            {"id": "claude-haiku-4-5-20251001", "name": "Haiku 4.5",
             "detail": "Fastest and cheapest; short mechanical work."},
        ],
    },
    {
        "id": "codex",
        "name": "Codex",
        "default_model": "gpt-5.6-sol",
        "models": [
            {"id": "gpt-6-astra", "name": "GPT-6 Astra",
             "detail": "Complex reasoning and sustained coding work."},
            {"id": "gpt-5.6-sol", "name": "GPT-5.6 Sol",
             "detail": "Quality-first flagship — reasoning and hard coding."},
            {"id": "gpt-5.6-terra", "name": "GPT-5.6 Terra",
             "detail": "Balanced quality, latency and cost."},
            {"id": "gpt-5.6-luna", "name": "GPT-5.6 Luna",
             "detail": "High-throughput, lower-latency work."},
            {"id": "gpt-5.5", "name": "GPT-5.5",
             "detail": "Previous generation flagship."},
            {"id": "gpt-5.4", "name": "GPT-5.4",
             "detail": "Older generation; kept for comparison runs."},
        ],
    },
]

_BY_ID = {e["id"]: e for e in ENGINES}


def engine_ids() -> list[str]:
    return [e["id"] for e in ENGINES]


def is_engine(engine: str) -> bool:
    return engine in _BY_ID


def models_for(engine: str) -> list[str]:
    return [m["id"] for m in _BY_ID[engine]["models"]]


def roster() -> dict:
    """The catalogue the app renders its pickers from, plus the fleet default
    so a device with no per-agent preference yet shows the same thing the host
    would spawn."""
    return {"engines": ENGINES, "default_engine": FLEET_ENGINE}


def base_agent(agent_id: str) -> str:
    """Preferences key on the agent, not the seat — `ops-chat` and
    `ops-service-call` are one agent's seats and share the choice. Same
    rule the notification mutes use."""
    return agent_id.split("-", 1)[0] if agent_id else agent_id


def _load() -> dict:
    try:
        d = json.loads(_STATE.read_text())
    except (OSError, json.JSONDecodeError):
        d = {}
    d.setdefault("agents", {})
    return d


def _save(d: dict) -> None:
    _STATE.parent.mkdir(parents=True, exist_ok=True)
    _STATE.write_text(json.dumps(d, indent=1))


def defaults(agent_id: str) -> dict:
    """This agent's choices, fully resolved — never a partial answer.

    Every engine in the roster gets a model here whether or not it was ever
    chosen, because the long-press menu can launch ANY engine, not just the
    agent's default one. A caller asking for Codex on a Claude-default agent
    still needs a Codex model, and "the one this agent uses for Codex" has to
    exist before it is picked."""
    stored = _load()["agents"].get(base_agent(agent_id), {})
    engine = stored.get("engine")
    if not is_engine(engine or ""):
        engine = FLEET_ENGINE
    picked = stored.get("models") or {}
    models = {}
    for e in ENGINES:
        chosen = picked.get(e["id"])
        models[e["id"]] = (chosen if chosen in models_for(e["id"])
                           else e["default_model"])
    return {"agent_id": base_agent(agent_id), "engine": engine,
            "models": models}


def set_defaults(agent_id: str, engine: str | None = None,
                 models: dict | None = None) -> dict:
    """Write the parts named, leave the rest. Raises ValueError on anything
    the roster doesn't know — a preference that can't be spawned is worse than
    no preference, because it fails at the one moment the user is waiting on a
    session to come up.

    A choice equal to the fleet/engine default is still STORED rather than
    dropped: dropping it would mean a later change to `ENGINES` silently
    re-decides something the user already decided by hand."""
    if engine is not None and not is_engine(engine):
        raise ValueError(f"unknown engine {engine!r}")
    for eng, model in (models or {}).items():
        if not is_engine(eng):
            raise ValueError(f"unknown engine {eng!r}")
        if model not in models_for(eng):
            raise ValueError(f"unknown {eng} model {model!r}")
    key = base_agent(agent_id)
    with _lock:
        d = _load()
        cur = d["agents"].setdefault(key, {})
        if engine is not None:
            cur["engine"] = engine
        if models:
            cur.setdefault("models", {}).update(models)
        _save(d)
    return defaults(agent_id)


def resolve(agent_id: str, engine: str | None = None,
            model: str | None = None) -> tuple[str, str]:
    """(engine, model) for a spawn. The single fallback point.

    `engine=None` → the agent's default engine. `model=None` → the agent's
    model for whichever engine was resolved, so long-pressing to the
    non-default engine still lands on a model the user chose for it. Anything
    named and unknown raises rather than falling back."""
    if engine is not None:
        engine = engine.strip().lower()
        if not is_engine(engine):
            raise ValueError(f"unknown engine {engine!r}")
    d = defaults(agent_id)
    engine = engine or d["engine"]
    if model is not None:
        model = model.strip()
        if model not in models_for(engine):
            raise ValueError(f"unknown {engine} model {model!r}")
        return engine, model
    return engine, d["models"][engine]


def model_flag(engine: str, model: str) -> str:
    """The CLI argument that pins the model, quoted for the pane's shell.

    Claude takes `--model`, Codex takes `-m`. The value is quoted here rather
    than at each call site because a model id is free-form vendor text: the
    `[1m]` suffix ids this roster used to carry were glob patterns to zsh, and
    the next id with a shell metacharacter will arrive the same way — as a
    session that comes up on the wrong model with no error anywhere."""
    if not model:
        return ""
    flag = "-m" if engine == "codex" else "--model"
    return f"{flag} '{model}'"
