/* Theme engine extracted from the supplied index.html. MIT Copyright (c) 2026 AI-paca; see THIRD_PARTY_NOTICES.md. Scoped to the workbench root. */
export function createWorkbenchTheme(root){
      const key = 'dialogue-theme-settings';
      const preference = query => { try { return typeof window.matchMedia === 'function' ? window.matchMedia(query) : null; } catch (_) { return null; } };
      const system = preference('(prefers-color-scheme: dark)');
      const systemLight = preference('(prefers-color-scheme: light)');
      const deviceScheme = () => system?.matches ? 'dark' : 'light';
      const clamp = (value, min = 0, max = 1) => Math.max(min, Math.min(max, value));
      const fields = ['base_background', 'dropzone_normal', 'dropzone_active', 'border', 'text_primary', 'text_secondary', 'accent_feedback'];
      const themes = [
        ['OLED Pro Black', '#07090B', '#101215', '#1A1D22', '#20242A', '#E7EAED', '#9AA2AB', '#58A6FF'],
        ['VS Code Modern', '#121314', '#191A1B', '#242526', '#2A2B2C', '#BBBEBF', '#8C8C8C', '#3994BC'],
        ['VS Code Dark+', '#1E1E1E', '#252526', '#2D2D30', '#3C3C3C', '#D4D4D4', '#A6A6A6', '#007ACC'],
        ['GitHub Dark Dimmed', '#24292F', '#2C333A', '#37404A', '#444C56', '#D7DEE6', '#A9B4C0', '#6EA6F8'],
        ['JetBrains Darcula', '#2B2B2B', '#333333', '#3D3D3D', '#4A4A4A', '#DADADA', '#A9A9A9', '#6897BB'],
        ['One Dark Pro', '#30343F', '#383D49', '#444A58', '#515969', '#E4E7ED', '#ABB2BF', '#61AFEF'],
        ['Monokai Graphite', '#3A3A32', '#44443A', '#515145', '#5F5F50', '#F4F1E4', '#C3BFA4', '#A6E22E'],
        ['Dimmed Workbench', '#555A62', '#60666F', '#6C737D', '#78818C', '#F0F2F4', '#C7CDD4', '#8FA1B3'],
        ['Classic Workbench', '#7C838C', '#8B929B', '#9AA1AA', '#A7ADB5', '#151A20', '#323A43', '#6F7F8F'],
        ['Soft Light Workbench', '#B8BEC7', '#AEB5BF', '#9FA8B4', '#9BA3AE', '#202326', '#4B5563', '#4C6F93'],
        ['VS Code Light', '#E6E8EC', '#D9DDE3', '#C7CED8', '#C6CCD4', '#202326', '#5A626D', '#3B6EA8'],
        ['Pure White', '#F4F6F8', '#E6EAEE', '#CBD6E0', '#D3D9DF', '#202326', '#5F666E', '#2F5F8F']
      ].map(([name, ...colors]) => ({ name, ...Object.fromEntries(fields.map((field, index) => [field, colors[index]])) }));
      const rgb = hex => { const number = parseInt(hex.slice(1), 16); return [(number >> 16) & 255, (number >> 8) & 255, number & 255]; };
      const hex = values => '#' + values.map(value => Math.round(clamp(value, 0, 255)).toString(16).padStart(2, '0')).join('');
      const mix = (a, b, amount) => { const left = rgb(a); const right = rgb(b); return hex(left.map((value, index) => value + (right[index] - value) * amount)); };
      const lighten = (color, amount) => mix(color, '#ffffff', amount);
      const darken = (color, amount) => mix(color, '#000000', amount);
      const luminance = color => rgb(color).map(value => { const channel = value / 255; return channel <= .03928 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4; }).reduce((total, value, index) => total + value * [.2126, .7152, .0722][index], 0);
      const contrast = (a, b) => (Math.max(luminance(a), luminance(b)) + .05) / (Math.min(luminance(a), luminance(b)) + .05);
      function readable(seed, backgrounds, ratio = 4.5) {
        const score = color => Math.min(...backgrounds.map(background => contrast(color, background)));
        if (score(seed) >= ratio) return seed;
        const destination = score('#111419') >= score('#f5f7fa') ? '#000000' : '#ffffff';
        let low = 0; let high = 1;
        for (let iteration = 0; iteration < 20; iteration++) {
          const middle = (low + high) / 2;
          if (score(mix(seed, destination, middle)) >= ratio) high = middle; else low = middle;
        }
        return mix(seed, destination, high);
      }
      function toHsl(color) {
        const [r, g, b] = rgb(color).map(value => value / 255);
        const high = Math.max(r, g, b); const low = Math.min(r, g, b);
        const lightness = (high + low) / 2;
        if (high === low) return [0, 0, lightness * 100];
        const delta = high - low;
        const saturation = lightness > .5 ? delta / (2 - high - low) : delta / (high + low);
        const hue = high === r ? ((g - b) / delta + (g < b ? 6 : 0)) * 60 : high === g ? ((b - r) / delta + 2) * 60 : ((r - g) / delta + 4) * 60;
        return [hue, saturation * 100, lightness * 100];
      }
      function fromHsl(hue, saturation, lightness) {
        const h = ((hue % 360) + 360) % 360;
        const s = clamp(saturation / 100); const l = clamp(lightness / 100);
        const c = (1 - Math.abs(2 * l - 1)) * s;
        const x = c * (1 - Math.abs((h / 60) % 2 - 1)); const m = l - c / 2;
        const channels = h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x] : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x];
        return hex(channels.map(value => (value + m) * 255));
      }
      function paletteAt(level) {
        const position = clamp(level) * (themes.length - 1);
        const index = Math.min(themes.length - 1, Math.floor(position));
        const fraction = position - index;
        if (fraction < .0000001 || index === themes.length - 1) return { ...themes[index] };
        const a = themes[index]; const b = themes[index + 1];
        const smooth = fraction * fraction * (3 - 2 * fraction);
        const background = mix(a.base_background, b.base_background, smooth);
        const [h, s, l] = toHsl(background); const dark = l < 50;
        return {
          name: fraction < .5 ? a.name : b.name,
          base_background: background,
          dropzone_normal: fromHsl(h, s, dark ? l + 4 : l - 4),
          dropzone_active: fromHsl(h, s + 6, dark ? l + 8 : l - 9),
          border: fromHsl(h, s, dark ? l + 8 : l - 10),
          text_primary: fromHsl(h, s * .5, dark ? 92 : 14),
          text_secondary: fromHsl(h, s * .5, dark ? 68 : 40),
          accent_feedback: mix(a.accent_feedback, b.accent_feedback, smooth)
        };
      }
      const defaults = () => ({ level: 10 / 11, auto: true, remember: true });
      let settings = defaults();
      try {
        const saved = JSON.parse(localStorage.getItem(key));
        if (saved && saved.version === 1) {
          settings.remember = saved.remember !== false;
          if (settings.remember) {
            if (Number.isFinite(saved.level)) settings.level = clamp(saved.level);
            settings.auto = saved.auto !== false;
          }
        } else {
          const legacy = localStorage.getItem('dialogue-theme');
          if (legacy === 'dark' || legacy === 'light') { settings.auto = false; settings.level = legacy === 'dark' ? 1 / 11 : 10 / 11; }
        }
      } catch (_) {}
      const effectiveLevel = () => settings.auto ? (deviceScheme() === 'dark' ? 1 / 11 : 10 / 11) : settings.level;
      const snapshot = () => { const level = effectiveLevel(); return { ...settings, deviceScheme: deviceScheme(), preferenceKnown: Boolean(system?.matches || systemLight?.matches), effectiveLevel: level, palette: paletteAt(level) }; };
      function persist() {
        try {
          localStorage.setItem(key, JSON.stringify(settings.remember ? { version: 1, ...settings } : { version: 1, remember: false }));
          localStorage.removeItem('dialogue-theme'); localStorage.removeItem('dialogue-accent');
        } catch (_) {}
      }
      function deriveTokens(palette) {
        const bg = palette.base_background;
        const dark = luminance(bg) < .179;
        // Keep every reading surface on the same side of the contrast crossover.
        const surface = color => {
          const destination = dark ? '#000000' : '#ffffff';
          let result = color;
          for (let step = 0; step < 100 && (dark ? luminance(result) > .175 : luminance(result) < .185); step++) result = mix(result, destination, .025);
          return result;
        };
        const panel = surface(palette.dropzone_normal);
        const hover = surface(mix(panel, dark ? '#ffffff' : '#000000', .045));
        const inset = surface(mix(bg, panel, .5));
        const backgrounds = [bg, panel, hover, inset];
        const foreground = dark ? '#edf0f3' : '#161b22';
        const primary = readable(foreground, backgrounds, 7);
        const secondary = readable(mix(primary, panel, .2), backgrounds, 4.7);
        const muted = readable(mix(primary, panel, .3), backgrounds, 4.5);
        const accentSoft = surface(mix(panel, palette.accent_feedback, .08));
        const accent = readable(palette.accent_feedback, [...backgrounds, accentSoft], 4.5);
        const border = mix(panel, primary, .23);
        const status = seed => {
          const soft = surface(mix(panel, seed, .075));
          const text = readable(seed, [...backgrounds, soft], 4.5);
          return { base: text, soft, border: mix(soft, text, .3), text };
        };
        const success = status(dark ? '#4ade80' : '#16a34a');
        const warning = status(dark ? '#fbbf24' : '#d97706');
        const danger = status(dark ? '#f87171' : '#dc2626');
        const info = status(dark ? '#818cf8' : '#6366f1');
        const tokens = {
          accent, 'accent-hover': readable(mix(accent, primary, .12), backgrounds, 4.5),
          'accent-soft': accentSoft,
          'accent-soft-border': mix(accentSoft, accent, .38),
          'accent-strong': accent,
          'accent-ring': accent + '80',
          'bg-app': bg, 'bg-surface': panel, 'bg-elevated': hover,
          'bg-inset': inset,
          'bg-overlay': dark ? 'rgba(0,0,0,.5)' : 'rgba(17,24,39,.4)',
          'text-primary': primary, 'text-secondary': secondary,
          'text-muted': muted, 'text-faint': muted,
          'text-on-accent': contrast('#111419', accent) > contrast('#ffffff', accent) ? '#111419' : '#ffffff', 'text-link': accent,
          'border-default': border, 'border-strong': mix(panel, primary, .4),
          'border-subtle': mix(panel, primary, .14), 'border-accent': mix(accent, border, .5)
        };
        for (const [name, values] of Object.entries({ success, warning, danger, info })) {
          tokens[name] = values.base;
          for (const suffix of ['soft', 'border', 'text']) tokens[`${name}-${suffix}`] = values[suffix];
        }
        return tokens;
      }
      function apply() {
        const view = snapshot(); const tokens = deriveTokens(view.palette);
        const aliases = {
          app: 'bg-app', panel: 'bg-surface', hover: 'bg-elevated', inset: 'bg-inset',
          text: 'text-primary', secondary: 'text-secondary', muted: 'text-muted',
          line: 'border-default', 'line-strong': 'border-strong', accent: 'accent',
          'accent-hover': 'accent-hover', 'accent-soft': 'accent-soft', 'accent-line': 'accent-soft-border',
          'on-accent': 'text-on-accent', green: 'success-text', 'green-soft': 'success-soft', 'green-line': 'success-border',
          warning: 'warning-text', 'warning-soft': 'warning-soft', danger: 'danger-text'
        };
        for (const [name, value] of Object.entries(tokens)) root.style.setProperty(`--color-${name}`, value);
        for (const [name, token] of Object.entries(aliases)) root.style.setProperty(`--${name}`, tokens[token]);
        const dark = luminance(view.palette.base_background) < .179;
        root.dataset.theme = dark ? 'dark' : 'light'; root.classList.toggle('dark', dark);
        root.removeAttribute('data-accent'); root.style.colorScheme = dark ? 'dark' : 'light';
        root.style.setProperty('--shadow', dark ? '0 20px 70px #0005, 0 3px 12px #0003' : '0 16px 60px #242a3e18, 0 3px 12px #242a3e0a');
        root.style.setProperty('--accent-shadow', tokens.accent + '30');
        root.style.setProperty('--theme-position', `${view.effectiveLevel * 100}%`);
        
        root.dispatchEvent(new CustomEvent('dialogue-themechange', { detail: view }));
      }
      function setLevel(value) { const number = Number(value); if (!Number.isFinite(number)) return; settings.level = clamp(number); settings.auto = false; persist(); apply(); }
      function setAuto(value) { if (!value) settings.level = effectiveLevel(); settings.auto = Boolean(value); persist(); apply(); }
      function setRemember(value) { settings.remember = Boolean(value); persist(); apply(); }
      function reset() { settings = { ...defaults(), remember: settings.remember }; persist(); apply(); }
      const onMedia=()=>{if(settings.auto)apply()};
      for(const media of [system,systemLight])media?.addEventListener?.('change',onMedia);
      const ramp = 'linear-gradient(90deg,' + Array.from({ length: 96 }, (_, index) => `${paletteAt(index / 95).base_background} ${(index / 95 * 100).toFixed(2)}%`).join(',') + ')';
      root.style.setProperty('--theme-ramp', ramp);
      apply();
      function audit() {
        const failures = [];
        for (let sample = 0; sample <= 1000; sample++) {
          const tokens = deriveTokens(paletteAt(sample / 1000));
          for (const text of ['text-primary', 'text-secondary', 'text-muted', 'accent']) {
            for (const background of ['bg-app', 'bg-surface', 'bg-inset', 'bg-elevated']) {
              const ratio = contrast(tokens[text], tokens[background]);
              if (ratio < 4.49) failures.push({ level: sample / 10, text, background, ratio });
            }
          }
        }
        return { samples: 1001, failures };
      }
      return { dispose:()=>{for(const media of [system,systemLight])media?.removeEventListener?.('change',onMedia)}, get: snapshot, paletteAt, deriveTokens, setLevel, setAuto, setRemember, reset, apply, rgb, contrast, audit };
}
