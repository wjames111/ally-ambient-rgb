import {
  PanelSection,
  PanelSectionRow,
  ToggleField,
  SliderField,
  staticClasses,
} from "@decky/ui";
import { callable, definePlugin } from "@decky/api";
import { useEffect, useState } from "react";
import { FaLightbulb } from "react-icons/fa";

interface Settings {
  enabled: boolean;
  sat_boost: number;
  ema: number;
  norm_max: number;
}

const startEngine = callable<[], boolean>("start");
const stopEngine = callable<[], boolean>("stop");
const isRunning = callable<[], boolean>("is_running");
const getSettings = callable<[], Settings>("get_settings");
const setSetting = callable<[key: string, value: number], boolean>("set_setting");

function Content() {
  const [ready, setReady] = useState(false);
  const [on, setOn] = useState(false);
  const [sat, setSat] = useState(1.5);
  const [ema, setEma] = useState(0.25);
  const [bright, setBright] = useState(210);

  useEffect(() => {
    (async () => {
      const s = await getSettings();
      setSat(s.sat_boost);
      setEma(s.ema);
      setBright(s.norm_max);
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
