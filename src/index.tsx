import {
  PanelSection,
  PanelSectionRow,
  ToggleField,
  SliderField,
  DropdownItem,
  staticClasses,
} from "@decky/ui";
import { callable, definePlugin } from "@decky/api";
import { useEffect, useState } from "react";
import { FaLightbulb } from "react-icons/fa";

interface Settings {
  enabled: boolean;
  mode: "unified" | "split" | "quad";
  sat_boost: number;
  ema: number;
  norm_max: number;
  stick_gain: number;
}

const startEngine = callable<[], boolean>("start");
const stopEngine = callable<[], boolean>("stop");
const isRunning = callable<[], boolean>("is_running");
const getSettings = callable<[], Settings>("get_settings");
const setSetting = callable<[key: string, value: number], boolean>("set_setting");
const setMode = callable<[mode: string], boolean>("set_mode");
const canPerZone = callable<[], boolean>("can_per_zone");

function Content() {
  const [ready, setReady] = useState(false);
  const [on, setOn] = useState(false);
  const [perZone, setPerZone] = useState(false);
  const [mode, setModeState] = useState<string>("unified");
  const [sat, setSat] = useState(1.5);
  const [ema, setEma] = useState(0.25);
  const [bright, setBright] = useState(210);
  const [stick, setStick] = useState(0.4);

  useEffect(() => {
    (async () => {
      const s = await getSettings();
      setModeState(s.mode);
      setSat(s.sat_boost);
      setEma(s.ema);
      setBright(s.norm_max);
      setStick(s.stick_gain);
      setPerZone(await canPerZone());
      setOn(await isRunning());
      setReady(true);
    })();
  }, []);

  const toggle = async (value: boolean) => {
    setOn(value);
    if (value) {
      await startEngine();
    } else {
      await stopEngine();
    }
  };

  const modeOptions = perZone
    ? [
        { data: "unified", label: "Unified (whole screen)" },
        { data: "split", label: "Split (left / right)" },
        { data: "quad", label: "Quad (four corners)" },
      ]
    : [{ data: "unified", label: "Unified (whole screen)" }];

  return (
    <PanelSection title="Ambient Lighting">
      <PanelSectionRow>
        <ToggleField
          label="Enabled"
          description="Rings follow the dominant color on screen"
          checked={on}
          onChange={toggle}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <DropdownItem
          label="Mode"
          description={
            perZone ? undefined : "Split / Quad need a one-time `sudo ./decky-setup.sh`"
          }
          rgOptions={modeOptions}
          selectedOption={mode}
          disabled={!ready || !perZone}
          onChange={(opt) => {
            setModeState(opt.data as string);
            setMode(opt.data as string);
          }}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <SliderField
          label="Vividness"
          value={sat}
          min={1.0}
          max={2.5}
          step={0.1}
          showValue
          disabled={!ready}
          onChange={(v) => {
            setSat(v);
            setSetting("sat_boost", v);
          }}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <SliderField
          label="Reactivity"
          value={ema}
          min={0.05}
          max={0.6}
          step={0.05}
          showValue
          disabled={!ready}
          onChange={(v) => {
            setEma(v);
            setSetting("ema", v);
          }}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <SliderField
          label="Brightness"
          value={bright}
          min={120}
          max={255}
          step={5}
          showValue
          disabled={!ready}
          onChange={(v) => {
            setBright(v);
            setSetting("norm_max", v);
          }}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <SliderField
          label="Joystick boost"
          description="Rings flare brighter as you push the sticks (0 = off)"
          value={stick}
          min={0}
          max={1}
          step={0.05}
          showValue
          disabled={!ready}
          onChange={(v) => {
            setStick(v);
            setSetting("stick_gain", v);
          }}
        />
      </PanelSectionRow>
    </PanelSection>
  );
}

export default definePlugin(() => ({
  name: "Flicker",
  titleView: <div className={staticClasses.Title}>Flicker</div>,
  content: <Content />,
  icon: <FaLightbulb />,
  onDismount() {},
}));
