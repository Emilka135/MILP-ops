# Рабочее место регистратора: источник оформления

Спокойная палитра и расположение панелей адаптированы по предоставленному пользователем эталону `adjust-web-page-styles (1).zip` (`index.html`, 316396 bytes). В эталоне palette ramp и liquid-glass shader происходят из:

- https://github.com/AI-paca/IttM/tree/main/web/src/ui/theme
- https://github.com/AI-paca/IttM/blob/main/web/src/ui/settings/ThemeSlider.tsx

В продукт перенесены движок палитр с коррекцией контраста, непрерывный CSS-ползунок и компоновка визуальных компонентов; код выделен в React-компоненты и независимый модуль темы. Рекурсивное дерево панелей и волна настоящего аудио реализованы для приложения. WebGL-шейдер ползунка не переносился. Вымышленные роли, демоданные, sql.js, SQLite в браузере, speechSynthesis и подставные записи эталона не перенесены. Уведомление исходного эталона сохранено ниже.

MIT License
Copyright (c) 2026 AI-paca

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
