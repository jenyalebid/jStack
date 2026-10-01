"""A machine never speaks as the user (rules-stage/prompt-sourcing.md).

Every user turn a hook, spawn or nudge sends opens with one shared tag, applied
where the text is delivered. The plugin and the host each carry the helper
because the host ships without the plugin tree; this pins the two together and
pins each delivery point to it.
"""
import importlib.util
import subprocess
from pathlib import Path

from jstack_host import prompt_files

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "plugins" / "jstack"


def _plugin_prompts():
    spec = importlib.util.spec_from_file_location("_plugin_prompts", PLUGIN / "hooks" / "_prompts.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_plugin_and_host_share_one_tag_and_one_rule():
    plugin = _plugin_prompts()
    assert plugin.INJECTED_TAG == prompt_files.INJECTED_TAG == "[system prompt]"
    for text in ("begin working", "  begin working", "[system prompt] x", "/compact", "", "   "):
        assert plugin.tagged(text) == prompt_files.tagged(text)


def test_tagged_is_idempotent_and_leaves_commands_bare():
    t = prompt_files.tagged
    assert t("begin working") == "[system prompt] begin working"
    assert t(t("begin working")) == "[system prompt] begin working"
    assert t("/compact") == "/compact"
    assert t("") == ""


def test_host_nudges_are_tagged():
    from jstack_host import compact_delivery, router
    for text in (compact_delivery.CONTINUE, compact_delivery.CONTINUE_IN_PLACE,
                 router.TAKEOVER_CONTINUE):
        assert text.startswith("[system prompt] ")


def test_portable_adapter_tags_the_first_prompt_once(tmp_path):
    """The plugin's adapter is the delivery point for every hook and skill
    spawn; it must tag an untagged kick, never stack a second, and leave a
    slash command bare. The tag block is lifted out of the script and run
    alone, so the test opens no terminal."""
    adapter = (PLUGIN / "bin" / "open-terminal-here").read_text()
    block = adapter[adapter.index('INJECTED_TAG="[system prompt]"'):]
    block = block[:block.index("\nfi\n") + 4]
    for given, want in (("begin working", "[system prompt] begin working"),
                        ("[system prompt] begin working", "[system prompt] begin working"),
                        ("/jstack:report", "/jstack:report")):
        out = subprocess.run(["bash", "-c", f'FIRST_PROMPT="$1"\n{block}\nprintf %s "$FIRST_PROMPT"',
                              "_", given], capture_output=True, text=True, check=True).stdout
        assert out == want
