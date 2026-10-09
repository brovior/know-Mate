"""Settings UI submissions preserve direct configuration values."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from knowmate.app.bridge import Bridge
from knowmate import config


@pytest.mark.parametrize("seconds", [1800, 3600, 5400, 7200, 9000, 10800])
def test_bridge_accepts_six_ui_choices(monkeypatch, seconds):
    patches = []
    monkeypatch.setattr(config, "update_settings", patches.append)
    assert json.loads(Bridge().saveSettings(json.dumps({"collector": {"idle_seconds": seconds}})))["ok"]
    assert patches[0]["collector"]["idle_seconds"] == seconds


@pytest.mark.parametrize("seconds", [60, 2700, 14400, True, "1800", 1800.5, None])
def test_bridge_rejects_non_ui_idle_submission(monkeypatch, seconds):
    monkeypatch.setattr(config, "update_settings", lambda _: pytest.fail("invalid patch saved"))
    assert not json.loads(Bridge().saveSettings(json.dumps({"collector": {"idle_seconds": seconds}})))["ok"]


@pytest.mark.parametrize("seconds", [60, 2700, 14400])
def test_general_config_updates_allow_direct_values(monkeypatch, seconds):
    cfg = {"collector": {"idle_seconds": 60}}
    monkeypatch.setattr(config, "get_config", lambda: cfg)
    monkeypatch.setattr(config, "_save_config", lambda _: None)
    config.update_settings({"collector": {"idle_seconds": seconds}})
    assert cfg["collector"]["idle_seconds"] == seconds
    assert json.loads(Bridge().saveSettings('{"collector":{"idle_enabled":false}}'))["ok"]
    assert cfg["collector"]["idle_seconds"] == seconds


def test_old_config_backfills_discovery_default_from_bundle(tmp_path, monkeypatch):
    user_config = tmp_path / "config.yaml"
    user_config.write_text("collector: {}\nmail: {}\n", encoding="utf-8")
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "_get_config_path", lambda: user_config)
    assert config.get_config()["mail"]["discovery_refresh_seconds"] == config.mail_discovery_refresh_seconds({})
    assert "discovery_refresh_seconds" in user_config.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node unavailable for UI execution")
def test_ui_preserves_custom_values_until_deliberate_selection():
    ui = Path(__file__).parents[1] / "app" / "ui"
    source = (ui / "app.js").read_text(encoding="utf-8")
    settings_functions = source[source.index("function _fillSettingsForm"):source.index("function testConnection")]
    script = r'''
const vm = require("vm");
const assert = require("assert");
const nodes = {};
function element(id) {
  return nodes[id] ||= {
    value: "", checked: false, dataset: {}, textContent: "",
    querySelectorAll: () => [], classList: {toggle: () => {}},
  };
}
const select = element("setIdleMinutes");
select.options = [30, 60, 90, 120, 150, 180].map(value => ({value: String(value)}));
select.querySelector = () => select.options.find(option => option.dataset?.custom);
select.prepend = option => {
  option.remove = () => { select.options = select.options.filter(item => item !== option); };
  select.options.unshift(option);
};
let saved;
const context = {
  document: {getElementById: element, querySelector: () => null},
  bridge: {getVersion: () => Promise.resolve("test"), saveSettings: payload => {saved = JSON.parse(payload); return Promise.resolve('{"ok":true}');}},
  Option: function(label, value) {this.text = label; this.value = value; this.dataset = {};},
  showToast: () => {}, closeSettings: () => {},
};
vm.createContext(context);
vm.runInContext(SOURCE, context);
for (const seconds of [60, 2700, 14400, 3600]) {
  context._fillSettingsForm({collector: {idle_seconds: seconds}});
  context.saveSettings();
  assert(!Object.hasOwn(saved.collector, "idle_seconds"));
  if (seconds !== 3600) {
    assert.equal(select.value, "custom");
    assert(select.options[0].disabled);
    assert(select.options[0].text.includes("직접 설정한 값"));
  }
  for (const minutes of [30, 60, 90, 120, 150, 180]) {
    select.value = String(minutes);
    select.onchange();
    context.saveSettings();
    assert.equal(saved.collector.idle_seconds, minutes * 60);
  }
}
'''.replace("SOURCE", json.dumps(settings_functions))
    subprocess.run([shutil.which("node"), "-e", script], check=True, capture_output=True, text=True)
    html = (ui / "index.html").read_text(encoding="utf-8")
    select_html = html.split('<select id="setIdleMinutes">', 1)[1].split("</select>", 1)[0]
    import re
    assert re.findall(r'<option value="(\d+)"', select_html) == ["30", "60", "90", "120", "150", "180"]
