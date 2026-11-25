// SPDX-License-Identifier: GPL-2.0-or-later
// Layout, colors and metrics follow the 1.6.0 widget toolkit (share/gui.c) and the screens built on it.
//! Retained widget tree, layout and drawing for the menus and the heads-up display.
//!
//! Widgets are added in *display order*: `hstack`/`harray` children run left to right and
//! `vstack`/`varray` children run top to bottom. Gradient text is drawn from a cached
//! white-text render target tinted in vertical bands, because the pinned macroquad cannot clip.
use crate::assets::{Assets, Result};
use macroquad::prelude::*;
use std::collections::HashMap;

pub(crate) mod col {
    use macroquad::prelude::Color;
    const fn rgb(r: u8, g: u8, b: u8) -> Color {
        Color { r: r as f32 / 255.0, g: g as f32 / 255.0, b: b as f32 / 255.0, a: 1.0 }
    }
    pub const WHT: Color = rgb(0xff, 0xff, 0xff);
    pub const YEL: Color = rgb(0xff, 0xff, 0x00);
    pub const RED: Color = rgb(0xff, 0x00, 0x00);
    pub const GRN: Color = rgb(0x00, 0xff, 0x00);
    pub const BLU: Color = rgb(0x00, 0x00, 0xff);
    pub const BLK: Color = rgb(0x00, 0x00, 0x00);
    pub const GRY: Color = rgb(0x55, 0x55, 0x55);
}

/// Text size classes: one 26th, 13th and 7th of the window's short side.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(crate) enum Size {
    Sml,
    Med,
    Lrg,
}

/// Rounded-corner flags of a background plate.
pub(crate) mod rect {
    pub const NW: u8 = 1;
    pub const NE: u8 = 2;
    pub const SE: u8 = 4;
    pub const SW: u8 = 8;
    pub const TOP: u8 = NW | NE;
    pub const ALL: u8 = 15;
}

/// What a state widget reports when it is activated: a screen-defined action and value.
pub(crate) type Token = (u32, i32);

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Kind {
    HStack,
    VStack,
    HArray,
    VArray,
    Filler,
    Space,
    Label,
    Image,
    Count,
    Clock,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Trunc {
    None,
    Tail,
    Head,
}

pub(crate) struct Widget {
    kind: Kind,
    kids: Vec<usize>,
    x: f32,
    y: f32,
    w: f32,
    h: f32,
    rect: u8,
    tok: Option<Token>,
    hilite: bool,
    fill: bool,
    trunc: Trunc,
    text: String,
    size: Size,
    c0: Color,
    c1: Color,
    value: i32,
    max: i32,
    tex: Option<Texture2D>,
    iw: f32,
    ih: f32,
    scale: f32,
}

impl Widget {
    fn new(kind: Kind) -> Self {
        Self {
            kind,
            kids: Vec::new(),
            x: 0.0,
            y: 0.0,
            w: 0.0,
            h: 0.0,
            rect: 0,
            tok: None,
            hilite: false,
            fill: false,
            trunc: Trunc::None,
            text: String::new(),
            size: Size::Sml,
            c0: col::YEL,
            c1: col::RED,
            value: 0,
            max: 0,
            tex: None,
            iw: 0.0,
            ih: 0.0,
            scale: 1.0,
        }
    }
}

fn same(a: Color, b: Color) -> bool {
    a.r == b.r && a.g == b.g && a.b == b.b
}

/// Shortens `text` with an ellipsis at the chosen end until it fits `room`.
fn fit(ui: &Ui, text: &str, px: f32, room: f32, trunc: Trunc) -> String {
    if trunc == Trunc::None || ui.measure(text, px) <= room {
        return text.to_owned();
    }
    let chars: Vec<char> = text.chars().collect();
    for keep in (0..chars.len()).rev() {
        let candidate: String = match trunc {
            Trunc::Tail => chars[..keep].iter().chain(['.', '.', '.'].iter()).collect(),
            _ => ['.', '.', '.'].iter().chain(chars[chars.len() - keep..].iter()).collect(),
        };
        if ui.measure(&candidate, px) <= room {
            return candidate;
        }
    }
    String::new()
}

fn mix(a: Color, b: Color, t: f32) -> Color {
    Color::new(a.r + (b.r - a.r) * t, a.g + (b.g - a.g) * t, a.b + (b.b - a.b) * t, 1.0)
}

/// Window metrics, the font, the plate textures and the gradient-text cache.
pub(crate) struct Ui {
    pub font: Font,
    theme: Vec<Texture2D>,
    grad: HashMap<(String, u16), (RenderTarget, f32, f32)>,
    sw: f32,
    sh: f32,
}

const GRADIENT_BANDS: usize = 12;
const GRADIENT_CACHE_LIMIT: usize = 400;

impl Ui {
    pub fn new(assets: &mut Assets) -> Result<Self> {
        let font = load_ttf_font_from_bytes(&assets.bytes("ttf/DejaVuSans-Bold.ttf")?)?;
        let mut theme = Vec::with_capacity(4);
        for name in ["plain", "plain-focus", "hilite", "hilite-focus"] {
            let themed = format!("gui/classic/back-{name}.png");
            let plain = format!("gui/back-{name}.png");
            let path = if assets.contains(&themed) { themed } else { plain };
            theme.push(assets.texture(&path)?);
        }
        Ok(Self { font, theme, grad: HashMap::new(), sw: 800.0, sh: 600.0 })
    }

    /// Latches the window size for the frame; call before building or drawing widgets.
    pub fn begin(&mut self) {
        self.sw = screen_width();
        self.sh = screen_height();
    }

    pub fn size(&self) -> (f32, f32) {
        (self.sw, self.sh)
    }

    fn short(&self) -> f32 {
        self.sw.min(self.sh)
    }

    pub fn pad(&self) -> f32 {
        self.short() / 60.0
    }

    pub fn px(&self, size: Size) -> f32 {
        self.short()
            / match size {
                Size::Sml => 26.0,
                Size::Med => 13.0,
                Size::Lrg => 7.0,
            }
    }

    fn fp(px: f32) -> u16 {
        px.round().clamp(1.0, 600.0) as u16
    }

    pub fn measure(&self, text: &str, px: f32) -> f32 {
        if text.is_empty() {
            return 0.0;
        }
        measure_text(text, Some(&self.font), Self::fp(px), 1.0).width
    }

    /// A ready-made plain text draw for code outside the widget tree (e.g. status lines).
    pub fn plain(&self, text: &str, size: Size, x: f32, baseline: f32, color: Color) {
        let fp = Self::fp(self.px(size));
        draw_text_ex(text, x, baseline, TextParams { font: Some(&self.font), font_size: fp, color, ..Default::default() });
    }

    fn rt(&mut self, text: &str, fp: u16) -> (Texture2D, f32, f32) {
        let key = (text.to_owned(), fp);
        if let Some((rt, w, h)) = self.grad.get(&key) {
            return (rt.texture.clone(), *w, *h);
        }
        if self.grad.len() >= GRADIENT_CACHE_LIMIT {
            self.grad.clear();
        }
        let px = fp as f32;
        let margin = (px * 0.1).ceil() + 2.0;
        let width = measure_text(text, Some(&self.font), fp, 1.0).width + 2.0 * margin;
        let height = (px * 1.164).ceil();
        let dpi = screen_dpi_scale();
        let target = render_target((width * dpi).ceil().max(1.0) as u32, (height * dpi).ceil().max(1.0) as u32);
        target.texture.set_filter(FilterMode::Linear);
        let mut camera = Camera2D::from_display_rect(Rect::new(0.0, 0.0, width, height));
        camera.render_target = Some(target.clone());
        set_camera(&camera);
        clear_background(Color::new(1.0, 1.0, 1.0, 0.0));
        draw_text_ex(text, margin, px * 0.928, TextParams { font: Some(&self.font), font_size: fp, color: WHITE, ..Default::default() });
        set_default_camera();
        let texture = target.texture.clone();
        self.grad.insert(key, (target, width, height));
        (texture, width, height)
    }

    /// Draws `text` centered on (`cx`, `cy`): a gradient from `c1` (top) to `c0` (bottom),
    /// with the 1.6.0 drop shadow. `scale` is the pulse factor.
    pub fn text(&mut self, text: &str, px: f32, cx: f32, cy: f32, c0: Color, c1: Color, scale: f32, shadow: bool) {
        if text.is_empty() {
            return;
        }
        let fp = Self::fp(px);
        let box_h = fp as f32 * 1.164 * scale;
        let flat = same(c0, c1);
        if flat && (scale - 1.0).abs() < 0.001 {
            let width = measure_text(text, Some(&self.font), fp, 1.0).width;
            let x = cx - width / 2.0;
            let base = cy - box_h / 2.0 + fp as f32 * 0.928;
            if shadow {
                let d = box_h / 16.0;
                draw_text_ex(text, x + d, base + d, TextParams { font: Some(&self.font), font_size: fp, color: Color::new(0.0, 0.0, 0.0, 0.5), ..Default::default() });
            }
            draw_text_ex(text, x, base, TextParams { font: Some(&self.font), font_size: fp, color: c0, ..Default::default() });
            return;
        }
        let (texture, rw, rh) = self.rt(text, fp);
        let (dw, dh) = (rw * scale, rh * scale);
        let (x0, y0) = (cx - dw / 2.0, cy - dh / 2.0);
        let size = texture.size();
        if shadow {
            let d = box_h / 16.0;
            draw_texture_ex(
                &texture,
                x0 + d,
                y0 + d,
                Color::new(0.0, 0.0, 0.0, 0.5),
                DrawTextureParams { dest_size: Some(vec2(dw, dh)), flip_y: true, ..Default::default() },
            );
        }
        let bands = if flat { 1 } else { GRADIENT_BANDS };
        let source_h = size.y / bands as f32;
        for k in 0..bands {
            let t = (k as f32 + 0.5) / bands as f32;
            let color = if flat { c0 } else { mix(c1, c0, t) };
            draw_texture_ex(
                &texture,
                x0,
                y0 + dh * k as f32 / bands as f32,
                color,
                DrawTextureParams {
                    dest_size: Some(vec2(dw, dh / bands as f32)),
                    source: Some(Rect::new(0.0, size.y - (k + 1) as f32 * source_h, size.x, source_h)),
                    flip_y: true,
                    ..Default::default()
                },
            );
        }
    }

    /// Draws a plate. `style` is `(hilite ? 2 : 0) | (focus ? 1 : 0)`; `corners` are the rounded ones.
    pub fn plate(&self, x: f32, y: f32, w: f32, h: f32, corners: u8, style: usize) {
        let texture = &self.theme[style.min(3)];
        let size = texture.size();
        let r = self.pad().min(w / 2.0).min(h / 2.0);
        let xs = [x, x + r, x + w - r, x + w];
        let ys = [y, y + r, y + h - r, y + h];
        let us = [0.0, 0.25, 0.75, 1.0];
        for iy in 0..3 {
            for ix in 0..3 {
                let (dw, dh) = (xs[ix + 1] - xs[ix], ys[iy + 1] - ys[iy]);
                if dw <= 0.0 || dh <= 0.0 {
                    continue;
                }
                let bit = match (ix, iy) {
                    (0, 0) => Some(rect::NW),
                    (2, 0) => Some(rect::NE),
                    (2, 2) => Some(rect::SE),
                    (0, 2) => Some(rect::SW),
                    _ => None,
                };
                let (u0, u1, v0, v1) = if bit.is_some_and(|b| corners & b == 0) {
                    (0.4, 0.6, 0.4, 0.6)
                } else {
                    (us[ix], us[ix + 1], us[iy], us[iy + 1])
                };
                draw_texture_ex(
                    texture,
                    xs[ix],
                    ys[iy],
                    WHITE,
                    DrawTextureParams {
                        dest_size: Some(vec2(dw, dh)),
                        source: Some(Rect::new(u0 * size.x, v0 * size.y, (u1 - u0) * size.x, (v1 - v0) * size.y)),
                        ..Default::default()
                    },
                );
            }
        }
    }
}

/// A layout of widgets for one screen or overlay.
pub(crate) struct Tree {
    w: Vec<Widget>,
    active: Option<usize>,
    pressed: Option<usize>,
}

impl Tree {
    /// Starts a tree whose root is a vertical stack.
    pub fn vstack() -> Self {
        Self { w: vec![Widget::new(Kind::VStack)], active: None, pressed: None }
    }

    pub fn hstack() -> Self {
        Self { w: vec![Widget::new(Kind::HStack)], active: None, pressed: None }
    }

    pub fn root(&self) -> usize {
        0
    }

    fn add(&mut self, parent: usize, widget: Widget) -> usize {
        let id = self.w.len();
        self.w.push(widget);
        self.w[parent].kids.push(id);
        id
    }

    pub fn hstack_in(&mut self, p: usize) -> usize {
        self.add(p, Widget::new(Kind::HStack))
    }
    pub fn vstack_in(&mut self, p: usize) -> usize {
        self.add(p, Widget::new(Kind::VStack))
    }
    pub fn harray(&mut self, p: usize) -> usize {
        self.add(p, Widget::new(Kind::HArray))
    }
    pub fn varray(&mut self, p: usize) -> usize {
        self.add(p, Widget::new(Kind::VArray))
    }
    pub fn filler(&mut self, p: usize) -> usize {
        self.add(p, Widget::new(Kind::Filler))
    }
    pub fn space(&mut self, p: usize) -> usize {
        self.add(p, Widget::new(Kind::Space))
    }

    /// A text label; `colors` is `(bottom, top)` and defaults to yellow over red.
    pub fn label(&mut self, p: usize, text: &str, size: Size, colors: Option<(Color, Color)>) -> usize {
        let mut w = Widget::new(Kind::Label);
        w.text = text.to_owned();
        w.size = size;
        if let Some((c0, c1)) = colors {
            w.c0 = c0;
            w.c1 = c1;
        }
        self.add(p, w)
    }

    /// A white button with a plate.
    pub fn state(&mut self, p: usize, text: &str, size: Size, token: Token) -> usize {
        let id = self.label(p, text, size, Some((col::WHT, col::WHT)));
        self.w[id].tok = Some(token);
        self.w[id].rect = rect::ALL;
        id
    }

    /// A highlighted button that takes the initial focus.
    pub fn start(&mut self, p: usize, text: &str, size: Size, token: Token) -> usize {
        let id = self.state(p, text, size, token);
        self.w[id].hilite = true;
        self.active = Some(id);
        id
    }

    /// Wrapped text: one label per `\`-separated line, on a shared plate.
    pub fn multi(&mut self, p: usize, text: &str, size: Size, c0: Color, c1: Color) -> usize {
        let id = self.vstack_in(p);
        let mut lines: Vec<&str> = text.split('\\').collect();
        if lines.len() > 1 && lines.last().is_some_and(|l| l.is_empty()) {
            lines.pop();
        }
        for line in lines {
            self.label(id, line, size, Some((c0, c1)));
        }
        self.w[id].rect = rect::ALL;
        id
    }

    pub fn image(&mut self, p: usize, tex: Option<Texture2D>, w: f32, h: f32) -> usize {
        let mut widget = Widget::new(Kind::Image);
        widget.tex = tex;
        widget.iw = w;
        widget.ih = h;
        self.add(p, widget)
    }

    /// A right-aligned digit counter sized for up to `max`; negative values draw nothing.
    pub fn count(&mut self, p: usize, value: i32, max: i32, size: Size, colors: Option<(Color, Color)>) -> usize {
        let mut w = Widget::new(Kind::Count);
        w.value = value;
        w.max = max;
        w.size = size;
        if let Some((c0, c1)) = colors {
            w.c0 = c0;
            w.c1 = c1;
        }
        self.add(p, w)
    }

    /// A `m:ss` clock with hundredths, in centiseconds; negative values draw nothing.
    pub fn clock(&mut self, p: usize, value: i32, size: Size) -> usize {
        let mut w = Widget::new(Kind::Clock);
        w.value = value;
        w.size = size;
        self.add(p, w)
    }

    pub fn set_rect(&mut self, id: usize, flags: u8) {
        self.w[id].rect = flags;
    }
    pub fn set_hilite(&mut self, id: usize, on: bool) {
        self.w[id].hilite = on;
    }
    pub fn set_colors(&mut self, id: usize, c0: Color, c1: Color) {
        self.w[id].c0 = c0;
        self.w[id].c1 = c1;
    }
    pub fn set_fill(&mut self, id: usize) {
        self.w[id].fill = true;
    }
    pub fn trunc_tail(&mut self, id: usize) {
        self.w[id].trunc = Trunc::Tail;
    }
    pub fn trunc_head(&mut self, id: usize) {
        self.w[id].trunc = Trunc::Head;
    }
    pub fn pulse(&mut self, id: usize, scale: f32) {
        self.w[id].scale = scale;
    }
    pub fn set_label(&mut self, id: usize, text: &str) {
        self.w[id].text = text.to_owned();
    }
    /// Turns a button into inert gray text.
    pub fn disable(&mut self, id: usize) {
        self.w[id].tok = None;
        self.w[id].c0 = col::GRY;
        self.w[id].c1 = col::GRY;
    }

    // ---- focus and input ----------------------------------------------------------------

    pub fn active_token(&self) -> Option<Token> {
        self.active.and_then(|i| self.w[i].tok)
    }

    /// Moves the focus to the button carrying `token`, if there is one.
    pub fn focus_token(&mut self, token: Token) {
        if let Some(i) = self.w.iter().position(|w| w.tok == Some(token)) {
            self.active = Some(i);
        }
    }

    /// Makes sure something is focused when any button exists.
    pub fn ensure_focus(&mut self) {
        if self.active.is_none() {
            self.active = self.w.iter().position(|w| w.tok.is_some());
        }
    }

    fn hit(&self, x: f32, y: f32) -> Option<usize> {
        self.w
            .iter()
            .enumerate()
            .find(|(_, w)| w.tok.is_some() && x >= w.x && x < w.x + w.w && y >= w.y && y < w.y + w.h)
            .map(|(i, _)| i)
    }

    /// The pointer moved: focus the button under it.
    pub fn point(&mut self, x: f32, y: f32) {
        if let Some(i) = self.hit(x, y) {
            self.active = Some(i);
        }
    }

    pub fn press(&mut self, x: f32, y: f32) {
        self.pressed = self.hit(x, y);
    }

    /// A click completes when the button is released over the one it was pressed on.
    pub fn release(&mut self, x: f32, y: f32) -> Option<Token> {
        let down = self.pressed.take()?;
        (self.hit(x, y) == Some(down)).then(|| self.w[down].tok).flatten()
    }

    pub fn activate(&self) -> Option<Token> {
        self.active_token()
    }

    /// Moves the focus to the nearest button in a direction (`dx`, `dy` are -1, 0 or 1).
    pub fn navigate(&mut self, dx: f32, dy: f32) {
        let Some(a) = self.active else {
            self.ensure_focus();
            return;
        };
        let (ax, ay) = (self.w[a].x + self.w[a].w / 2.0, self.w[a].y + self.w[a].h / 2.0);
        let mut best: Option<(f32, usize)> = None;
        for (i, w) in self.w.iter().enumerate() {
            if i == a || w.tok.is_none() {
                continue;
            }
            let (ox, oy) = (w.x + w.w / 2.0 - ax, w.y + w.h / 2.0 - ay);
            let along = ox * dx + oy * dy;
            if along <= 1.0 {
                continue;
            }
            let across = (ox * dy).abs() + (oy * dx).abs();
            let score = along + 3.0 * across;
            if best.is_none_or(|(s, _)| score < s) {
                best = Some((score, i));
            }
        }
        if let Some((_, i)) = best {
            self.active = Some(i);
        }
    }

    /// Decays pulses by `dt` seconds toward 1.
    pub fn tick(&mut self, dt: f32) {
        for w in &mut self.w {
            if w.scale > 1.0 {
                w.scale = (w.scale - dt).max(1.0);
            }
        }
    }

    // ---- layout -------------------------------------------------------------------------

    fn up(&mut self, ui: &Ui, i: usize) -> (f32, f32) {
        let pad = ui.pad();
        let kids = self.w[i].kids.clone();
        let (w, h) = match self.w[i].kind {
            Kind::Filler => (0.0, 0.0),
            Kind::Space => (pad * 2.0, pad * 2.0),
            Kind::Label => {
                let px = ui.px(self.w[i].size);
                let th = (1.164 * px).round();
                let mut tw = ui.measure(&self.w[i].text, px);
                if tw > 0.0 && tw < th {
                    tw = th;
                }
                (tw + pad, th + pad)
            }
            Kind::Count => {
                let px = ui.px(self.w[i].size);
                let digits = self.w[i].max.max(1).to_string().len() as f32;
                (digits * ui.measure("0", px) + pad, (1.164 * px).round() + pad)
            }
            Kind::Clock => {
                let px = ui.px(self.w[i].size);
                (6.0 * ui.measure("0", px) + pad, (1.164 * px).round() + pad)
            }
            Kind::Image => (self.w[i].iw, self.w[i].ih),
            Kind::HStack | Kind::VStack | Kind::HArray | Kind::VArray => {
                let sizes: Vec<(f32, f32)> = kids.iter().map(|&k| self.up(ui, k)).collect();
                let max_w = sizes.iter().map(|s| s.0).fold(0.0, f32::max);
                let max_h = sizes.iter().map(|s| s.1).fold(0.0, f32::max);
                let n = sizes.len() as f32;
                match self.w[i].kind {
                    Kind::HStack => (sizes.iter().map(|s| s.0).sum(), max_h),
                    Kind::VStack => (max_w, sizes.iter().map(|s| s.1).sum()),
                    Kind::HArray => (max_w * n, max_h),
                    _ => (max_w, max_h * n),
                }
            }
        };
        self.w[i].w = w;
        self.w[i].h = h;
        (w, h)
    }

    fn is_fill(&self, i: usize) -> bool {
        self.w[i].kind == Kind::Filler || self.w[i].fill
    }

    fn dn(&mut self, i: usize, x: f32, y: f32, w: f32, h: f32) {
        self.w[i].x = x;
        self.w[i].y = y;
        self.w[i].w = w;
        self.w[i].h = h;
        let kids = self.w[i].kids.clone();
        let n = kids.len();
        if n == 0 {
            return;
        }
        match self.w[i].kind {
            Kind::HStack => {
                let fixed: f32 = kids.iter().filter(|&&k| !self.is_fill(k)).map(|&k| self.w[k].w).sum();
                let fills = kids.iter().filter(|&&k| self.is_fill(k)).count();
                let share = if fills > 0 { ((w - fixed) / fills as f32).max(0.0) } else { 0.0 };
                let used = if fills > 0 { w } else { fixed };
                let mut cx = x + (w - used) / 2.0;
                for &k in &kids {
                    let kw = if self.is_fill(k) { share } else { self.w[k].w };
                    self.dn(k, cx, y, kw, h);
                    cx += kw;
                }
            }
            Kind::VStack => {
                let fixed: f32 = kids.iter().filter(|&&k| !self.is_fill(k)).map(|&k| self.w[k].h).sum();
                let fills = kids.iter().filter(|&&k| self.is_fill(k)).count();
                let share = if fills > 0 { ((h - fixed) / fills as f32).max(0.0) } else { 0.0 };
                let used = if fills > 0 { h } else { fixed };
                let mut cy = y + (h - used) / 2.0;
                for &k in &kids {
                    let kh = if self.is_fill(k) { share } else { self.w[k].h };
                    self.dn(k, x, cy, w, kh);
                    cy += kh;
                }
            }
            Kind::HArray => {
                let cell = w / n as f32;
                for (j, &k) in kids.iter().enumerate() {
                    self.dn(k, x + cell * j as f32, y, cell, h);
                }
            }
            Kind::VArray => {
                let cell = h / n as f32;
                for (j, &k) in kids.iter().enumerate() {
                    self.dn(k, x, y + cell * j as f32, w, cell);
                }
            }
            _ => {}
        }
    }

    /// Sizes the tree and places it. `xd` and `yd` are -1 (left, bottom), 0 (centered) or 1
    /// (right, top). A root stack holding a filler along its axis spans the whole window.
    pub fn layout(&mut self, ui: &Ui, xd: i32, yd: i32) {
        let (sw, sh) = ui.size();
        let (mut w, mut h) = self.up(ui, 0);
        let kids = self.w[0].kids.clone();
        let has_filler = kids.iter().any(|&k| self.is_fill(k));
        match self.w[0].kind {
            Kind::HStack if has_filler => w = sw,
            Kind::VStack if has_filler => h = sh,
            _ => {}
        }
        let x = match xd {
            d if d < 0 => 0.0,
            d if d > 0 => sw - w,
            _ => (sw - w) / 2.0,
        };
        let y = match yd {
            d if d > 0 => 0.0,
            d if d < 0 => sh - h,
            _ => (sh - h) / 2.0,
        };
        self.dn(0, x, y, w, h);
    }

    // ---- drawing ------------------------------------------------------------------------

    pub fn draw(&self, ui: &mut Ui) {
        self.paint(ui, 0);
    }

    fn paint(&self, ui: &mut Ui, i: usize) {
        let w = &self.w[i];
        if w.rect != 0 {
            let style = if w.hilite { 2 } else { 0 } | usize::from(self.active == Some(i) && w.tok.is_some());
            ui.plate(w.x, w.y, w.w, w.h, w.rect, style);
        }
        let (cx, cy) = (w.x + w.w / 2.0, w.y + w.h / 2.0);
        match w.kind {
            Kind::Label => {
                let px = ui.px(w.size);
                let text = fit(ui, &w.text, px, w.w - ui.pad(), w.trunc);
                ui.text(&text, px, cx, cy, w.c0, w.c1, w.scale, true);
            }
            Kind::Count if w.value >= 0 => {
                let px = ui.px(w.size);
                let w0 = ui.measure("0", px);
                let digits = w.value.to_string();
                let mut x = cx + (w.max.max(1).to_string().len() as f32 * w0) / 2.0 - w0 / 2.0;
                for ch in digits.chars().rev() {
                    ui.text(&ch.to_string(), px, x, cy, w.c0, w.c1, w.scale, true);
                    x -= w0;
                }
            }
            Kind::Clock if w.value >= 0 => {
                let px = ui.px(w.size);
                let w0 = ui.measure("0", px);
                let (m, s, hs) = (w.value / 6000, w.value / 100 % 60, w.value % 100);
                let big = if m > 0 { format!("{m}:{s:02}") } else { format!("{s}") };
                let small = format!("{hs:02}");
                let small_px = px * 0.6;
                let slot = |c: char| if c == ':' { w0 * 0.5 } else { w0 };
                let big_w: f32 = big.chars().map(slot).sum();
                let small_slot = w0 * 0.6;
                let total = big_w + small_slot * 2.0;
                let mut x = cx - total / 2.0;
                for ch in big.chars() {
                    let sl = slot(ch);
                    ui.text(&ch.to_string(), px, x + sl / 2.0, cy, w.c0, w.c1, w.scale, true);
                    x += sl;
                }
                let drop = (px - small_px) * 0.5 * 1.164 * 0.5;
                for ch in small.chars() {
                    ui.text(&ch.to_string(), small_px, x + small_slot / 2.0, cy + drop, w.c0, w.c1, w.scale, true);
                    x += small_slot;
                }
            }
            Kind::Image => {
                if let Some(tex) = &w.tex {
                    draw_texture_ex(tex, cx - w.iw / 2.0, cy - w.ih / 2.0, WHITE, DrawTextureParams { dest_size: Some(vec2(w.iw, w.ih)), ..Default::default() });
                }
            }
            _ => {}
        }
        for &k in &w.kids {
            self.paint(ui, k);
        }
    }
}
