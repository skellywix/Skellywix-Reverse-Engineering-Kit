// SPDX-License-Identifier: GPL-2.0-or-later
// Screens, flow and controls follow Neverball 1.6.0 (ball/st_*.c, ball/hud.c, ball/progress.c).
//! The desktop game loop: title, level sets, briefing, play, results, options and help.
//!
//! Every screen rebuilds its widget tree each frame from the current state; focus survives the
//! rebuild as a token. Simulation runs only on the play screen (and for a moment after an
//! attempt ends, as in the original); menus and the pause screen never step the game.
use crate::assets::{Assets, Result};
use crate::audio::Audio;
use crate::render::{Renderer, Scene};
use crate::replays::{self, Capture};
use crate::shell_ui::{col, rect, Size, Token, Tree, Ui};
use macroquad::prelude::*;
use neverball::game::{CameraMode, Controls, Game};
use neverball::level::LevelInfo;
use neverball::progress::{LevelStart, Mode, Outcome as PrOutcome, Placement, Progress, RenameTarget, ScoreTable};
use neverball::set::Set;
use std::fs;
use std::path::{Path, PathBuf};

const A_PLAY: u32 = 1;
const A_REPLAY: u32 = 2;
const A_HELP: u32 = 3;
const A_OPTIONS: u32 = 4;
const A_EXIT: u32 = 5;
const A_BACK: u32 = 6;
const A_SET: u32 = 7;
const A_PREV: u32 = 8;
const A_NEXT_PAGE: u32 = 9;
const A_LEVEL: u32 = 10;
const A_CHALLENGE: u32 = 11;
const A_SCORE: u32 = 12;
const A_LOCK: u32 = 13;
const A_CHANGE: u32 = 14;
const A_FINISH: u32 = 16;
const A_NEXT: u32 = 17;
const A_RETRY: u32 = 18;
const A_LEAVE: u32 = 19;
const A_CONT: u32 = 20;
const A_QUIT: u32 = 21;
const A_KEY: u32 = 22;
const A_BKSP: u32 = 23;
const A_CAPS: u32 = 24;
const A_OK: u32 = 25;
const A_CANCEL: u32 = 26;
const A_MOUSE: u32 = 27;
const A_SOUND: u32 = 28;
const A_MUSIC: u32 = 29;
const A_PAGE: u32 = 30;
const A_SELECT: u32 = 31;
const A_SAVE_REPLAY: u32 = 32;

const SET_STEP: usize = 6;
const NAME_MAX: usize = 8;
const TITLE_LEVEL: &str = "map-medium/title.sol";
const HIDDEN_SET: &str = "set-misc.txt";
const MENU_MUSIC: &str = "bgm/inter.ogg";
const TITLE_MUSIC: &str = "bgm/title.ogg";
const SCORE_NAMES: [&str; 3] = ["Most Coins", "Best Times", "Fast Unlock"];
const MOUSE_DEFAULT: i32 = 300;
const TILT_MAX: f32 = 20.0;
const KEY_RESPONSE: f32 = 0.25;
const MOUSE_RESPONSE: f32 = 0.05;

struct TiltInput {
    angles: [f32; 2],
    keys: [i8; 2],
    response: f32,
}

impl Default for TiltInput {
    fn default() -> Self {
        Self { angles: [0.0; 2], keys: [0; 2], response: MOUSE_RESPONSE }
    }
}

impl TiltInput {
    fn sample(&mut self, keys: [i8; 2], mouse: [f32; 2], sensitivity: f32) {
        // In 1.6.0 an arrow is a stick-axis event, including key-up. Idle frames
        // don't synthesize mouse events or change how quickly the floor levels.
        for (axis, key) in keys.into_iter().enumerate() {
            if key != self.keys[axis] {
                self.angles[axis] = TILT_MAX * f32::from(key);
                self.response = KEY_RESPONSE;
            }
        }
        if mouse != [0.0; 2] {
            self.response = MOUSE_RESPONSE;
            for (angle, delta) in self.angles.iter_mut().zip(mouse) {
                *angle = (*angle + 2.0 * TILT_MAX * delta / sensitivity).clamp(-TILT_MAX, TILT_MAX);
            }
        }
        self.keys = keys;
    }
}

type Colors = Option<(Color, Color)>;

fn g(c0: Color, c1: Color) -> Colors {
    Some((c0, c1))
}

fn lerp3(a: [f32; 3], b: [f32; 3], k: f32) -> [f32; 3] {
    [a[0] + (b[0] - a[0]) * k, a[1] + (b[1] - a[1]) * k, a[2] + (b[2] - a[2]) * k]
}

fn add3(a: [f32; 3], b: [f32; 3]) -> [f32; 3] {
    [a[0] + b[0], a[1] + b[1], a[2] + b[2]]
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Scr {
    Title,
    Sets,
    Levels,
    Briefing,
    Ready,
    Play,
    Paused,
    Result,
    Over,
    Done,
    Name,
    ReplayName,
    OverwriteReplay,
    Options,
    Help,
}

struct Settings {
    player: String,
    mouse: i32,
    sound: i32,
    music: i32,
    lock_goals: bool,
}

impl Settings {
    fn load(path: &Path) -> Self {
        let mut s = Self { player: String::new(), mouse: MOUSE_DEFAULT, sound: 10, music: 10, lock_goals: true };
        let Ok(text) = fs::read_to_string(path) else { return s };
        for line in text.lines() {
            let Some((k, v)) = line.split_once(' ') else { continue };
            match k {
                "player" if valid_name(v) => s.player = v.to_owned(),
                "mouse" => s.mouse = v.parse().unwrap_or(MOUSE_DEFAULT).clamp(100, 600),
                "sound" => s.sound = v.parse().unwrap_or(10).clamp(0, 10),
                "music" => s.music = v.parse().unwrap_or(10).clamp(0, 10),
                "lock_goals" => s.lock_goals = v != "0",
                _ => {}
            }
        }
        s
    }

    fn store(&self, path: &Path) -> std::io::Result<()> {
        let text = format!(
            "player {}\nmouse {}\nsound {}\nmusic {}\nlock_goals {}\n",
            self.player,
            self.mouse,
            self.sound,
            self.music,
            u8::from(self.lock_goals)
        );
        fs::write(path, text)
    }
}

fn valid_name(name: &str) -> bool {
    !name.is_empty() && name.chars().count() <= NAME_MAX && name.chars().all(|c| c.is_ascii_graphic())
}

/// What the last attempt recorded, kept while its result screen is up.
struct Attempt {
    kind: PrOutcome,
    placement: Placement,
    at: f32,
    replay_ready: bool,
}

struct Shell {
    assets: Assets,
    ui: Ui,
    audio: Audio,
    renderer: Renderer,
    save_root: PathBuf,
    settings: Settings,
    back: Option<Texture2D>,

    sets: Vec<Set>,
    page: usize,
    cur_set: usize,
    infos: Vec<LevelInfo>,
    progress: Option<Progress>,
    score_type: usize,

    title: (Scene, Game),
    scene: Option<Scene>,
    game: Option<Game>,
    capture: Option<Capture>,
    info: Option<LevelInfo>,
    level_index: usize,
    orig_radius: f32,

    screen: Scr,
    since: f32,
    focus: Option<Token>,
    mouse: (f32, f32),
    grabbed: bool,
    music: String,
    want_music: String,
    notice: Option<(String, f32)>,
    quit: bool,
    replay_menu: bool,

    attempt: Option<Attempt>,
    paused_from: Scr,
    name_from: Scr,
    name_entry: String,
    caps: bool,
    help_page: usize,
    ready_cued: [bool; 2],

    tilt: TiltInput,
    cam: CameraMode,
    cam_label: Option<(f32, usize)>,
    go_t: f32,
    last_coins: i32,
    last_clock: i32,
    coin_pulse: (f32, f32),
    goal_pulse: (f32, f32),
}

pub(crate) async fn run(mut assets: Assets, save_root: &Path) -> Result<()> {
    let ui = Ui::new(&mut assets)?;
    let mut audio = Audio::new(&mut assets).await?;
    let renderer = Renderer::new(&mut assets)?;
    let settings = Settings::load(&save_root.join("settings.txt"));
    audio.volumes(settings.sound as f32 / 10.0, settings.music as f32 / 10.0);
    let sets: Vec<Set> = assets.sets()?.into_iter().filter(|s| s.file != HIDDEN_SET).collect();
    let level = assets.level(TITLE_LEVEL)?;
    let scene = Scene::load(&mut assets, &level)?;
    let game = Game::new(level, true);
    let back = if assets.contains("back/gui.jpg") { assets.texture("back/gui.jpg").ok() } else { None };

    let mut shell = Shell {
        assets,
        ui,
        audio,
        renderer,
        save_root: save_root.to_owned(),
        settings,
        back,
        sets,
        page: 0,
        cur_set: 0,
        infos: Vec::new(),
        progress: None,
        score_type: 0,
        title: (scene, game),
        scene: None,
        game: None,
        capture: None,
        info: None,
        level_index: 0,
        orig_radius: 0.0,
        screen: Scr::Title,
        since: 0.0,
        focus: None,
        mouse: mouse_position(),
        grabbed: false,
        music: String::new(),
        want_music: TITLE_MUSIC.to_owned(),
        notice: None,
        quit: false,
        replay_menu: false,
        attempt: None,
        paused_from: Scr::Play,
        name_from: Scr::Title,
        name_entry: String::new(),
        caps: true,
        help_page: 0,
        ready_cued: [false; 2],
        tilt: TiltInput::default(),
        cam: CameraMode::Chase,
        cam_label: None,
        go_t: 2.0,
        last_coins: 0,
        last_clock: 0,
        coin_pulse: (0.0, 1.0),
        goal_pulse: (0.0, 1.0),
    };
    loop {
        if is_quit_requested() || shell.quit {
            shell.release_cursor();
            return Ok(());
        }
        shell.frame().await?;
        next_frame().await;
    }
}

impl Shell {
    // ---- plumbing --------------------------------------------------------------------------

    fn go(&mut self, screen: Scr) {
        self.screen = screen;
        self.since = 0.0;
        self.focus = None;
        match screen {
            Scr::Title => self.want_music = TITLE_MUSIC.to_owned(),
            Scr::Sets | Scr::Levels | Scr::Over | Scr::Done | Scr::Options | Scr::Help => self.want_music = MENU_MUSIC.to_owned(),
            _ => {}
        }
    }

    fn say(&mut self, text: String) {
        self.notice = Some((text, 6.0));
    }

    fn release_cursor(&mut self) {
        if self.grabbed {
            set_cursor_grab(false);
            show_mouse(true);
            self.grabbed = false;
            self.mouse = mouse_position();
        }
    }

    fn grab_cursor(&mut self) {
        if !self.grabbed {
            set_cursor_grab(true);
            show_mouse(false);
            self.grabbed = true;
            self.mouse = mouse_position();
        }
    }

    fn save_settings(&mut self) {
        let path = self.save_root.join("settings.txt");
        if let Err(e) = self.settings.store(&path) {
            self.say(format!("Settings were not saved: {e}"));
        }
        self.audio.volumes(self.settings.sound as f32 / 10.0, self.settings.music as f32 / 10.0);
    }

    fn store_progress(&mut self) {
        if let Some(pr) = &self.progress {
            if let Err(e) = pr.store(&self.save_root) {
                self.say(format!("Progress was not saved: {e}"));
            }
        }
    }

    async fn frame(&mut self) -> Result<()> {
        let dt = get_frame_time().clamp(0.0, 0.25);
        self.ui.begin();
        self.since += dt;
        if let Some((_, t)) = &mut self.notice {
            *t -= dt;
        }
        if self.notice.as_ref().is_some_and(|(_, t)| *t <= 0.0) {
            self.notice = None;
        }
        if self.want_music != self.music {
            let path = self.want_music.clone();
            self.audio.music(&mut self.assets, &path).await?;
            self.music = path;
        }
        let playing = matches!(self.screen, Scr::Play) && self.attempt.is_none();
        if playing {
            self.grab_cursor();
        } else {
            self.release_cursor();
        }
        match self.screen {
            Scr::Title => self.title_screen(),
            Scr::Sets => self.sets_screen()?,
            Scr::Levels => self.levels_screen()?,
            Scr::Briefing => self.briefing_screen()?,
            Scr::Ready => self.ready_screen(dt)?,
            Scr::Play => self.play_screen(dt)?,
            Scr::Paused => self.pause_screen()?,
            Scr::Result => self.result_screen(dt)?,
            Scr::Over => self.over_screen()?,
            Scr::Done => self.done_screen()?,
            Scr::Name | Scr::ReplayName => self.name_screen()?,
            Scr::OverwriteReplay => self.overwrite_replay_screen(),
            Scr::Options => self.options_screen()?,
            Scr::Help => self.help_screen()?,
        }
        if let Some((text, _)) = &self.notice {
            let (w, h) = self.ui.size();
            let tw = self.ui.measure(text, self.ui.px(Size::Sml));
            self.ui.plain(text, Size::Sml, (w - tw) / 2.0, h - self.ui.pad() * 2.0, col::YEL);
        }
        if self.replay_menu {
            self.replay_menu = false;
            replays::browse(&mut self.assets, &self.save_root, &self.renderer, &mut self.audio, &self.ui.font).await?;
            self.focus = None;
            self.mouse = mouse_position();
        }
        Ok(())
    }

    /// Lays `tree` out, applies pointer and keyboard input and returns an activated token.
    fn pump(&mut self, tree: &mut Tree, xd: i32, yd: i32) -> Option<Token> {
        tree.layout(&self.ui, xd, yd);
        if let Some(f) = self.focus {
            tree.focus_token(f);
        }
        tree.ensure_focus();
        let before = tree.active_token();
        let (mx, my) = mouse_position();
        let mut act = None;
        if (mx, my) != self.mouse {
            tree.point(mx, my);
            self.mouse = (mx, my);
        }
        if is_mouse_button_pressed(MouseButton::Left) {
            tree.press(mx, my);
        }
        if is_mouse_button_released(MouseButton::Left) {
            act = tree.release(mx, my);
        }
        if is_key_pressed(KeyCode::Up) {
            tree.navigate(0.0, -1.0);
        }
        if is_key_pressed(KeyCode::Down) {
            tree.navigate(0.0, 1.0);
        }
        if is_key_pressed(KeyCode::Left) {
            tree.navigate(-1.0, 0.0);
        }
        if is_key_pressed(KeyCode::Right) {
            tree.navigate(1.0, 0.0);
        }
        if is_key_pressed(KeyCode::Enter) || is_key_pressed(KeyCode::KpEnter) {
            act = tree.activate();
        }
        let now = tree.active_token();
        if now != before && self.focus != now {
            self.audio.cue("menu", 1.0);
        }
        self.focus = now;
        if act.is_some() {
            self.audio.cue("select", 1.0);
        }
        act
    }

    fn backdrop(&self) {
        let (w, h) = self.ui.size();
        clear_background(Color::new(0.0, 0.0, 0.12, 1.0));
        if let Some(tex) = &self.back {
            draw_texture_ex(tex, 0.0, 0.0, WHITE, DrawTextureParams { dest_size: Some(vec2(w, h)), ..Default::default() });
        }
    }

    fn draw_level(&self, view: Option<([f32; 3], [f32; 3])>) {
        match (&self.scene, &self.game) {
            (Some(scene), Some(game)) => {
                self.renderer.draw(scene, game, view, get_time() as f32);
                set_default_camera();
            }
            _ => clear_background(BLACK),
        }
    }

    /// The level's flyover camera: `k` = 1 is the level overview, 0 the play camera.
    fn fly(&self, k: f32) -> Option<([f32; 3], [f32; 3])> {
        let game = self.game.as_ref()?;
        let cam = game.camera();
        let view = game.level().sol.views.first()?;
        Some((lerp3(cam.eye, view.position, k), lerp3(cam.center, view.target, k)))
    }

    // ---- title -------------------------------------------------------------------------------

    fn title_screen(&mut self) {
        let t = self.since;
        let (scene, game) = &self.title;
        let ball = game.ball().position;
        let p0 = add3(ball, [0.0, 0.75, 2.0]);
        let c0 = add3(ball, [0.0, 0.25, 0.0]);
        let k = (std::f32::consts::PI * t / 20.0).cos();
        let views = &game.level().sol.views;
        let far = if k >= 0.0 { views.first() } else { views.get(1).or(views.first()) };
        let (p1, c1) = far.map_or((p0, c0), |v| (v.position, v.target));
        let kk = k * k;
        let view = (lerp3(p0, p1, kk), lerp3(c0, c1, kk));
        self.renderer.draw(scene, game, Some(view), get_time() as f32);
        set_default_camera();

        let mut tree = Tree::vstack();
        let title = tree.label(0, "  Neverball  ", Size::Lrg, None);
        tree.set_rect(title, rect::ALL);
        tree.space(0);
        let list = tree.varray(0);
        tree.start(list, "Play", Size::Med, (A_PLAY, 0));
        tree.state(list, "Replay", Size::Med, (A_REPLAY, 0));
        tree.state(list, "Help", Size::Med, (A_HELP, 0));
        tree.state(list, "Options", Size::Med, (A_OPTIONS, 0));
        tree.state(list, "Exit", Size::Med, (A_EXIT, 0));
        let act = self.pump(&mut tree, 0, 0);
        tree.draw(&mut self.ui);
        match act {
            Some((A_PLAY, _)) if self.settings.player.is_empty() => self.open_name(Scr::Title),
            Some((A_PLAY, _)) => self.go(Scr::Sets),
            Some((A_REPLAY, _)) => self.replay_menu = true,
            Some((A_HELP, _)) => self.go(Scr::Help),
            Some((A_OPTIONS, _)) => self.go(Scr::Options),
            Some((A_EXIT, _)) => self.quit = true,
            _ => {}
        }
    }

    // ---- sets ----------------------------------------------------------------------------------

    fn sets_screen(&mut self) -> Result<()> {
        self.backdrop();
        let pages = self.sets.len().div_ceil(SET_STEP).max(1);
        self.page = self.page.min(pages - 1);
        let first = self.page * SET_STEP;
        let hover = match self.focus {
            Some((A_SET, i)) => Some(i as usize),
            _ => (first < self.sets.len()).then_some(first),
        };
        let (sw, sh) = self.ui.size();

        let mut tree = Tree::vstack();
        let head = tree.hstack_in(0);
        tree.start(head, "Back", Size::Sml, (A_BACK, 0));
        if pages > 1 {
            tree.state(head, "<", Size::Sml, (A_PREV, 0));
            tree.label(head, &format!("{}/{}", self.page + 1, pages), Size::Sml, g(col::WHT, col::WHT));
            tree.state(head, ">", Size::Sml, (A_NEXT_PAGE, 0));
        }
        tree.filler(head);
        let title = tree.label(head, "Level Set", Size::Sml, None);
        tree.set_rect(title, rect::ALL);
        tree.space(0);
        let row = tree.harray(0);
        let list = tree.varray(row);
        for slot in 0..SET_STEP {
            match self.sets.get(first + slot) {
                Some(set) => {
                    tree.state(list, &set.name, Size::Sml, (A_SET, (first + slot) as i32));
                }
                None => {
                    tree.label(list, " ", Size::Sml, None);
                }
            }
        }
        let shot = hover.and_then(|i| self.assets.texture(&self.sets[i].shot).ok());
        tree.image(row, shot, sw * 7.0 / 16.0, sh * 7.0 / 16.0);
        tree.space(0);
        let text = hover.map_or(String::new(), |i| self.sets[i].description.clone());
        tree.multi(0, if text.is_empty() { " " } else { &text }, Size::Sml, col::WHT, col::WHT);

        let act = self.pump(&mut tree, 0, 0);
        tree.draw(&mut self.ui);
        if is_key_pressed(KeyCode::Escape) {
            self.go(Scr::Title);
            return Ok(());
        }
        match act {
            Some((A_BACK, _)) => self.go(Scr::Title),
            Some((A_PREV, _)) => self.page = self.page.saturating_sub(1),
            Some((A_NEXT_PAGE, _)) => self.page = (self.page + 1).min(pages - 1),
            Some((A_SET, i)) => self.open_set(i as usize)?,
            _ => {}
        }
        Ok(())
    }

    fn open_set(&mut self, index: usize) -> Result<()> {
        let set = self.sets[index].clone();
        let mut infos = Vec::with_capacity(set.levels.len());
        for path in &set.levels {
            infos.push(self.assets.level(path)?.info);
        }
        let mut progress = Progress::new(&set, &infos)?;
        progress.set_lock_goals(self.settings.lock_goals);
        if let Err(e) = progress.load(&self.save_root) {
            if !progress.has_unreadable_save() {
                return Err(e.into());
            }
            let moved = progress.quarantine_unreadable_save(&self.save_root)?;
            let note = match moved {
                Some(path) => format!("The saved scores could not be read; kept as {}", path.display()),
                None => "The saved scores could not be read.".to_owned(),
            };
            self.say(note);
        }
        progress.new_game(Mode::Normal);
        self.cur_set = index;
        self.infos = infos;
        self.progress = Some(progress);
        self.score_type = 0;
        self.go(Scr::Levels);
        Ok(())
    }

    // ---- levels ---------------------------------------------------------------------------------

    fn levels_screen(&mut self) -> Result<()> {
        self.backdrop();
        let Some(pr) = self.progress.as_ref() else {
            self.go(Scr::Sets);
            return Ok(());
        };
        let set = &self.sets[self.cur_set];
        let hover_level = match self.focus {
            Some((A_LEVEL, i)) if pr.is_unlocked(i as usize) => Some(i as usize),
            _ => None,
        };
        let (sw, sh) = self.ui.size();

        let mut tree = Tree::vstack();
        let head = tree.hstack_in(0);
        tree.start(head, "Back", Size::Sml, (A_BACK, 0));
        tree.filler(head);
        let title = tree.label(head, &set.name, Size::Sml, None);
        tree.set_rect(title, rect::ALL);
        tree.space(0);

        let row = tree.harray(0);
        let grid = tree.varray(row);
        for r in 0..5 {
            let line = tree.harray(grid);
            for c in 0..5 {
                let i = r * 5 + c;
                match pr.level_label(i) {
                    Some(label) if pr.is_unlocked(i) => {
                        let fore = if pr.is_bonus(i) { col::GRN } else { col::WHT };
                        let back = if pr.is_completed(i) { fore } else { col::YEL };
                        let id = tree.state(line, &label, Size::Sml, (A_LEVEL, i as i32));
                        tree.set_colors(id, back, fore);
                    }
                    Some(label) => {
                        tree.label(line, &label, Size::Sml, None);
                    }
                    None => {
                        tree.label(line, " ", Size::Sml, g(col::BLK, col::BLK));
                    }
                }
            }
        }
        tree.state(grid, "Challenge", Size::Sml, (A_CHALLENGE, 0));
        let shot_name = match hover_level {
            Some(i) => self.infos.get(i).map(|x| x.shot.clone()),
            None => Some(set.shot.clone()),
        };
        let shot = shot_name.and_then(|n| self.assets.texture(&n).ok());
        tree.image(row, shot, sw * 7.0 / 16.0, sh * 5.0 / 16.0);
        tree.space(0);

        let board = tree.hstack_in(0);
        tree.filler(board);
        let table: Option<&ScoreTable> = match hover_level.and_then(|i| pr.level_scores(i)) {
            Some(s) => Some(match self.score_type {
                0 => &s.coin,
                1 => &s.time,
                _ => &s.goal,
            }),
            None => match self.score_type {
                0 => Some(&pr.set_scores().coin),
                1 => Some(&pr.set_scores().time),
                _ => None,
            },
        };
        score_box(&mut tree, board, SCORE_NAMES[self.score_type], table, None);
        tree.filler(board);
        let types = tree.vstack_in(board);
        tree.filler(types);
        for (k, name) in SCORE_NAMES.iter().enumerate() {
            let id = tree.state(types, name, Size::Sml, (A_SCORE, k as i32));
            tree.set_hilite(id, k == self.score_type);
        }
        tree.filler(types);
        tree.filler(board);
        tree.space(0);

        let lock = tree.hstack_in(0);
        tree.filler(lock);
        let l = tree.label(lock, "Goal State in Completed Levels", Size::Sml, g(col::WHT, col::WHT));
        tree.set_rect(l, rect::ALL);
        tree.space(lock);
        let pair = tree.harray(lock);
        let locked = tree.state(pair, "Locked", Size::Sml, (A_LOCK, 1));
        tree.set_hilite(locked, self.settings.lock_goals);
        let unlocked = tree.state(pair, "Unlocked", Size::Sml, (A_LOCK, 0));
        tree.set_hilite(unlocked, !self.settings.lock_goals);
        tree.filler(lock);

        let act = self.pump(&mut tree, 0, 0);
        tree.draw(&mut self.ui);
        let wheel = mouse_wheel().1;
        if is_key_pressed(KeyCode::Tab) || wheel != 0.0 {
            let step = if wheel > 0.0 { 2 } else { 1 };
            self.score_type = (self.score_type + step) % 3;
        }
        if is_key_pressed(KeyCode::Escape) {
            self.go(Scr::Sets);
            return Ok(());
        }
        match act {
            Some((A_BACK, _)) => self.go(Scr::Sets),
            Some((A_SCORE, k)) => self.score_type = k as usize,
            Some((A_LOCK, v)) => {
                self.settings.lock_goals = v != 0;
                if let Some(p) = &mut self.progress {
                    p.set_lock_goals(self.settings.lock_goals);
                }
                self.save_settings();
            }
            Some((A_LEVEL, i)) => {
                let p = self.progress.as_mut().ok_or("no set")?;
                p.new_game(Mode::Normal);
                let start = p.play(i as usize)?;
                self.begin_level(start, true)?;
            }
            Some((A_CHALLENGE, _)) => {
                let p = self.progress.as_mut().ok_or("no set")?;
                p.new_game(Mode::Challenge);
                let start = p.play(0)?;
                self.begin_level(start, true)?;
            }
            _ => {}
        }
        Ok(())
    }

    // ---- level start -----------------------------------------------------------------------------

    fn begin_level(&mut self, start: LevelStart, briefing: bool) -> Result<()> {
        let path = self.sets[self.cur_set].levels.get(start.index).cloned().ok_or("The level is missing from its set.")?;
        let level = self.assets.level(&path)?;
        let scene = Scene::load(&mut self.assets, &level)?;
        let game = Game::new(level.clone(), start.goal_open);
        let pr = self.progress.as_ref().ok_or("no set")?;
        let capture = Capture::new(&path, &game).with_context(pr.mode(), pr.balls(), pr.score(), pr.times());
        self.orig_radius = game.ball().radius;
        self.last_coins = game.coins();
        self.last_clock = game.clock_centis();
        self.want_music = level.info.song.clone();
        self.info = Some(level.info.clone());
        self.scene = Some(scene);
        self.game = Some(game);
        self.capture = Some(capture);
        self.level_index = start.index;
        self.attempt = None;
        self.tilt = TiltInput::default();
        self.cam = CameraMode::Chase;
        self.cam_label = None;
        self.coin_pulse = (0.0, 1.0);
        self.goal_pulse = (0.0, 1.0);
        self.ready_cued = [false; 2];
        self.go(if briefing { Scr::Briefing } else { Scr::Ready });
        Ok(())
    }

    fn briefing_screen(&mut self) -> Result<()> {
        self.draw_level(self.fly(1.0));
        let Some(pr) = self.progress.as_ref() else { return Ok(()) };
        let label = pr.level_label(self.level_index).unwrap_or_default();
        let bonus = pr.is_bonus(self.level_index);
        let challenge = pr.mode() == Mode::Challenge;
        let set_name = &self.sets[self.cur_set].name;
        let attr = if challenge { format!("{set_name}: Challenge") } else { set_name.clone() };
        let message = self.info.as_ref().map(|i| i.message.clone()).unwrap_or_default();

        let mut tree = Tree::vstack();
        let block = tree.vstack_in(0);
        if bonus {
            tree.label(block, &format!("Bonus Level {label}"), Size::Med, g(col::WHT, col::GRN));
        } else {
            tree.label(block, &format!("Level {label}"), Size::Lrg, None);
        }
        let a = tree.label(block, &attr, Size::Sml, g(col::WHT, col::WHT));
        tree.set_rect(a, rect::ALL);
        tree.space(0);
        tree.multi(0, if message.is_empty() { " " } else { &message }, Size::Sml, col::WHT, col::WHT);
        tree.layout(&self.ui, 0, 0);
        tree.draw(&mut self.ui);
        if is_key_pressed(KeyCode::Escape) {
            self.exit_level()?;
        } else if is_mouse_button_pressed(MouseButton::Left) || is_key_pressed(KeyCode::Enter) || is_key_pressed(KeyCode::KpEnter) || is_key_pressed(KeyCode::Space) {
            self.audio.cue("select", 1.0);
            self.go(Scr::Ready);
        }
        Ok(())
    }

    fn ready_screen(&mut self, _dt: f32) -> Result<()> {
        let t = self.since;
        let (word, k, step) = if t < 1.0 { ("Ready?", 1.0 - 0.5 * t, 0) } else { ("Set?", 0.5 - 0.5 * (t - 1.0), 1) };
        if !self.ready_cued[step] {
            self.ready_cued[step] = true;
            self.audio.cue(if step == 0 { "ready" } else { "set" }, 1.0);
        }
        self.draw_level(self.fly(k.max(0.0)));
        self.draw_hud(0.0);
        let frac = (t % 1.0).clamp(0.0, 1.0);
        let mut tree = Tree::vstack();
        let l = tree.label(0, word, Size::Lrg, g(col::YEL, col::RED));
        tree.pulse(l, 1.0 + 0.2 * (1.0 - frac));
        tree.layout(&self.ui, 0, 0);
        tree.draw(&mut self.ui);
        let skip = is_mouse_button_pressed(MouseButton::Left) || is_key_pressed(KeyCode::Enter) || is_key_pressed(KeyCode::Space);
        if is_key_pressed(KeyCode::Escape) {
            self.exit_level()?;
        } else if t >= 2.0 || skip {
            self.audio.cue("go", 1.0);
            self.go_t = 0.0;
            self.tilt = TiltInput::default();
            self.mouse = mouse_position();
            self.go(Scr::Play);
        }
        Ok(())
    }

    // ---- play -------------------------------------------------------------------------------------

    fn read_controls(&mut self) -> Controls {
        let (mx, my) = mouse_position();
        let (dx, dy) = (mx - self.mouse.0, my - self.mouse.1);
        self.mouse = (mx, my);
        let sense = self.settings.mouse.max(1) as f32;
        let up = is_key_down(KeyCode::Up);
        let down = is_key_down(KeyCode::Down);
        let left = is_key_down(KeyCode::Left);
        let right = is_key_down(KeyCode::Right);
        let keys = [i8::from(up) - i8::from(down), i8::from(right) - i8::from(left)];
        self.tilt.sample(keys, [-dy, dx], sense);

        let speed = if is_key_down(KeyCode::LeftShift) || is_key_down(KeyCode::RightShift) { 3.0 } else { 1.5 };
        let r = is_key_down(KeyCode::D) || is_mouse_button_down(MouseButton::Right);
        let l = is_key_down(KeyCode::S) || is_mouse_button_down(MouseButton::Left);
        let rotation = match (l, r) {
            (true, false) => -speed,
            (false, true) => speed,
            _ => 0.0,
        };
        let mut set_cam = None;
        if is_key_pressed(KeyCode::Key1) {
            set_cam = Some(CameraMode::Chase);
        }
        if is_key_pressed(KeyCode::Key2) {
            set_cam = Some(CameraMode::Lazy);
        }
        if is_key_pressed(KeyCode::Key3) {
            set_cam = Some(CameraMode::Manual);
        }
        if is_key_pressed(KeyCode::E) || is_mouse_button_pressed(MouseButton::Middle) {
            set_cam = Some(if self.cam == CameraMode::Manual { CameraMode::Chase } else { CameraMode::Manual });
        }
        if let Some(cam) = set_cam {
            self.cam = cam;
            let index = match cam {
                CameraMode::Chase => 0,
                CameraMode::Lazy => 1,
                CameraMode::Manual => 2,
            };
            self.cam_label = Some((2.0, index));
        }
        Controls { tilt_x: self.tilt.angles[0], tilt_z: self.tilt.angles[1], response: self.tilt.response, rotation, camera: self.cam }
    }

    fn play_screen(&mut self, dt: f32) -> Result<()> {
        if self.attempt.is_none() {
            if is_key_pressed(KeyCode::Escape) {
                self.pause(Scr::Play);
                return Ok(());
            }
            if is_key_pressed(KeyCode::R) && self.progress.as_ref().is_some_and(Progress::retry_available) {
                return self.retry();
            }
        } else if let Some(a) = &mut self.attempt {
            a.at += dt;
        }
        let controls = if self.attempt.is_none() { self.read_controls() } else { Controls::default() };
        self.go_t += dt;
        if let Some((t, _)) = &mut self.cam_label {
            *t -= dt;
        }
        if self.cam_label.is_some_and(|(t, _)| t <= 0.0) {
            self.cam_label = None;
        }
        self.coin_pulse.0 = (self.coin_pulse.0 - dt).max(0.0);
        self.goal_pulse.0 = (self.goal_pulse.0 - dt).max(0.0);

        let mut events = Vec::new();
        let Some(game) = self.game.as_mut() else { return Ok(()) };
        game.advance(dt, &controls, |e| events.push(e));
        if let Some(c) = &mut self.capture {
            for e in &events {
                c.events(e);
            }
            c.frame(game, dt);
        }
        let radius = game.ball().radius;
        let coins = game.coins();
        let clock = game.clock_centis();
        for e in &events {
            self.audio.events(*e, radius, self.orig_radius);
        }
        if self.attempt.is_none() {
            let gain = coins - self.last_coins;
            if gain > 0 {
                let peak = if gain >= 10 { 2.0 } else if gain >= 5 { 1.5 } else { 1.25 };
                self.coin_pulse = (1.0, peak);
                self.goal_pulse = (1.0, peak);
            }
            if clock <= 1000 && self.last_clock / 100 > clock / 100 && clock > 0 {
                self.audio.cue("tick", 1.5);
            }
            if clock < 500 && self.last_clock / 50 > clock / 50 && clock > 0 {
                self.audio.cue("tock", 1.25);
            }
        }
        self.last_coins = coins;
        self.last_clock = clock;

        if self.attempt.is_none() {
            if let Some(outcome) = events.iter().find_map(|e| e.outcome) {
                self.record_outcome(outcome)?;
            }
        }
        self.draw_level(None);
        self.draw_hud(self.go_t);
        if let Some(a) = &self.attempt {
            let delay = match a.kind {
                PrOutcome::Goal => 1.0,
                PrOutcome::Fall => 2.0,
                PrOutcome::Timeout => 0.0,
            };
            if a.at >= delay {
                if let (Some(capture), Some(game)) = (self.capture.take(), &self.game) {
                    match capture.save(&self.save_root, &self.settings.player, game) {
                        Ok(()) => {
                            if let Some(attempt) = &mut self.attempt {
                                attempt.replay_ready = true;
                            }
                        }
                        Err(e) => self.say(format!("The replay was not saved: {e}")),
                    }
                }
                self.go(Scr::Result);
            }
        }
        Ok(())
    }

    fn record_outcome(&mut self, outcome: neverball::game::Outcome) -> Result<()> {
        let Some(game) = self.game.as_ref() else { return Ok(()) };
        let clock = game.clock_centis();
        let coins = game.coins();
        let player = self.settings.player.clone();
        let pr = self.progress.as_mut().ok_or("no set")?;
        let rec = match outcome {
            neverball::game::Outcome::Goal => pr.record_goal(coins, clock, &player)?,
            neverball::game::Outcome::Fall => pr.record_fall(clock)?,
            neverball::game::Outcome::Time => pr.record_timeout(clock)?,
        };
        if rec.needs_save {
            self.store_progress();
        }
        self.attempt = Some(Attempt { kind: rec.outcome, placement: rec.placement, at: 0.0, replay_ready: false });
        Ok(())
    }

    fn retry(&mut self) -> Result<()> {
        let pr = self.progress.as_mut().ok_or("no set")?;
        let start = pr.retry()?;
        self.begin_level(start, false)
    }

    fn pause(&mut self, from: Scr) {
        if let Some(g) = &mut self.game {
            g.set_paused(true);
        }
        if let Some(c) = &mut self.capture {
            c.set_paused(true);
        }
        self.paused_from = from;
        self.go(Scr::Paused);
    }

    fn resume(&mut self) {
        if let Some(g) = &mut self.game {
            g.set_paused(false);
        }
        if let Some(c) = &mut self.capture {
            c.set_paused(false);
        }
        self.tilt = TiltInput::default();
        self.mouse = mouse_position();
        let back = self.paused_from;
        let keep = self.want_music.clone();
        self.go(back);
        self.want_music = keep;
    }

    fn pause_screen(&mut self) -> Result<()> {
        self.draw_level(None);
        self.draw_hud(2.0);
        let mut tree = Tree::vstack();
        let title = tree.label(0, "Paused", Size::Lrg, None);
        tree.set_rect(title, rect::ALL);
        tree.space(0);
        let row = tree.harray(0);
        tree.start(row, "Continue", Size::Sml, (A_CONT, 0));
        let retry = self.progress.as_ref().is_some_and(Progress::retry_available);
        if retry {
            tree.state(row, "Restart", Size::Sml, (A_RETRY, 0));
        } else {
            let l = tree.label(row, "Restart", Size::Sml, g(col::GRY, col::GRY));
            tree.set_rect(l, rect::ALL);
        }
        tree.state(row, "Quit", Size::Sml, (A_QUIT, 0));
        let act = self.pump(&mut tree, 0, 0);
        tree.draw(&mut self.ui);
        if is_key_pressed(KeyCode::Escape) {
            self.resume();
            return Ok(());
        }
        match act {
            Some((A_CONT, _)) => self.resume(),
            Some((A_RETRY, _)) => self.retry()?,
            Some((A_QUIT, _)) => self.exit_level()?,
            _ => {}
        }
        Ok(())
    }

    // ---- HUD ----------------------------------------------------------------------------------------

    fn draw_hud(&mut self, go_time: f32) {
        let Some(game) = self.game.as_ref() else { return };
        let Some(pr) = self.progress.as_ref() else { return };
        let coins = game.coins();
        let goal = pr.goal_remaining(coins).max(0);
        let clock = game.clock_centis();
        let challenge = pr.mode() == Mode::Challenge;
        let pulse = |p: (f32, f32)| 1.0 + (p.1 - 1.0) * p.0.clamp(0.0, 1.0);

        let mut right = Tree::hstack();
        right.set_rect(0, rect::NW);
        let nums = right.vstack_in(0);
        let c = right.count(nums, coins, 100, Size::Sml, g(col::YEL, col::RED));
        right.pulse(c, pulse(self.coin_pulse));
        let gl = right.count(nums, goal, 10, Size::Sml, g(col::YEL, col::RED));
        right.pulse(gl, pulse(self.goal_pulse));
        let names = right.vstack_in(0);
        right.label(names, "Coins", Size::Sml, g(col::WHT, col::WHT));
        right.label(names, "Goal", Size::Sml, g(col::WHT, col::WHT));
        right.layout(&self.ui, 1, -1);
        right.draw(&mut self.ui);

        if challenge {
            let mut left = Tree::hstack();
            left.set_rect(0, rect::NE);
            let nums = left.vstack_in(0);
            left.count(nums, pr.balls(), 10, Size::Sml, g(col::YEL, col::RED));
            left.count(nums, pr.score(), 1000, Size::Sml, g(col::YEL, col::RED));
            let names = left.vstack_in(0);
            left.label(names, "Balls", Size::Sml, g(col::WHT, col::WHT));
            left.label(names, "Score", Size::Sml, g(col::WHT, col::WHT));
            left.layout(&self.ui, -1, -1);
            left.draw(&mut self.ui);
        }

        let mut time = Tree::hstack();
        time.set_rect(0, rect::TOP);
        let low = clock <= 1000 && self.info.as_ref().is_some_and(|i| i.time > 0);
        let id = time.clock(0, clock, Size::Med);
        if low {
            time.set_colors(id, col::YEL, col::RED);
        }
        time.layout(&self.ui, 0, -1);
        time.draw(&mut self.ui);

        if let Some((_, index)) = self.cam_label {
            let name = ["Chase Camera", "Lazy Camera", "Manual Camera"][index];
            let mut cam = Tree::hstack();
            cam.set_rect(0, rect::SW);
            cam.label(0, name, Size::Sml, g(col::WHT, col::WHT));
            cam.layout(&self.ui, 1, 1);
            cam.draw(&mut self.ui);
        }
        if go_time < 1.0 && self.screen == Scr::Play && self.attempt.is_none() {
            let mut go = Tree::vstack();
            let l = go.label(0, "GO!", Size::Lrg, g(col::BLU, col::GRN));
            go.pulse(l, 1.0 + 0.2 * (1.0 - go_time));
            go.layout(&self.ui, 0, 0);
            go.draw(&mut self.ui);
        }
    }

    // ---- results --------------------------------------------------------------------------------------

    fn result_screen(&mut self, dt: f32) -> Result<()> {
        self.step_idle(dt);
        self.draw_level(None);
        let (Some(pr), Some(att)) = (self.progress.as_ref(), self.attempt.as_ref()) else {
            self.go(Scr::Levels);
            return Ok(());
        };
        let high = att.kind == PrOutcome::Goal && att.placement.is_high_score();
        let frac = self.since.clamp(0.0, 1.0);

        let mut tree = Tree::vstack();
        match att.kind {
            PrOutcome::Goal => {
                if high {
                    let l = tree.label(0, "New Record", Size::Med, g(col::GRN, col::GRN));
                    tree.pulse(l, 1.0 + 0.2 * (1.0 - frac));
                } else {
                    let l = tree.label(0, "GOAL", Size::Lrg, g(col::BLU, col::GRN));
                    tree.pulse(l, 1.0 + 0.2 * (1.0 - frac));
                }
            }
            PrOutcome::Fall => {
                let l = tree.label(0, "Fall-out!", Size::Lrg, g(col::GRY, col::RED));
                tree.pulse(l, 1.0 + 0.2 * (1.0 - frac));
            }
            PrOutcome::Timeout => {
                let l = tree.label(0, "Time's Up!", Size::Lrg, g(col::GRY, col::RED));
                tree.pulse(l, 1.0 + 0.2 * (1.0 - frac));
            }
        }
        tree.space(0);
        let level = self.level_index;
        if pr.mode() == Mode::Challenge {
            let stats = tree.hstack_in(0);
            let a = tree.vstack_in(stats);
            tree.count(a, pr.balls(), 10, Size::Med, g(col::YEL, col::RED));
            tree.label(a, "Balls", Size::Sml, g(col::WHT, col::WHT));
            let b = tree.vstack_in(stats);
            tree.count(b, pr.score(), 1000, Size::Med, g(col::YEL, col::RED));
            tree.label(b, "Score", Size::Sml, g(col::WHT, col::WHT));
            tree.space(0);
        }
        if att.kind == PrOutcome::Goal {
            let board = tree.hstack_in(0);
            tree.filler(board);
            let hi = match self.score_type {
                0 => att.placement.coin,
                1 => att.placement.time,
                _ => att.placement.goal,
            }
            .map(|r| r.index());
            let table = pr.level_scores(level).map(|s| match self.score_type {
                0 => &s.coin,
                1 => &s.time,
                _ => &s.goal,
            });
            score_box(&mut tree, board, SCORE_NAMES[self.score_type], table, hi);
            tree.filler(board);
            let types = tree.vstack_in(board);
            tree.filler(types);
            for (k, name) in SCORE_NAMES.iter().enumerate() {
                let id = tree.state(types, name, Size::Sml, (A_SCORE, k as i32));
                tree.set_hilite(id, k == self.score_type);
            }
            if high {
                tree.space(types);
                tree.state(types, "Change Name", Size::Sml, (A_CHANGE, 0));
            }
            tree.filler(types);
            tree.filler(board);
            tree.space(0);
        }
        let row = tree.harray(0);
        let finish = att.kind == PrOutcome::Goal && (pr.is_done() || pr.is_last() || !pr.next_available());
        if att.replay_ready {
            tree.state(row, "Save Replay", Size::Sml, (A_SAVE_REPLAY, 0));
        }
        if pr.retry_available() {
            tree.start(row, "Retry Level", Size::Sml, (A_RETRY, 0));
        }
        if pr.next_available() {
            tree.start(row, "Next Level", Size::Sml, (A_NEXT, 0));
        }
        if finish {
            tree.start(row, "Finish", Size::Sml, (A_FINISH, 0));
        } else if att.kind != PrOutcome::Goal && pr.is_dead() {
            tree.start(row, "Exit", Size::Sml, (A_LEAVE, 0));
        }
        let act = self.pump(&mut tree, 0, 0);
        tree.draw(&mut self.ui);
        if is_key_pressed(KeyCode::Escape) {
            return self.exit_level();
        }
        if is_key_pressed(KeyCode::R) && self.progress.as_ref().is_some_and(Progress::retry_available) {
            return self.retry();
        }
        match act {
            Some((A_SCORE, k)) => self.score_type = k as usize,
            Some((A_CHANGE, _)) => self.open_name(Scr::Result),
            Some((A_SAVE_REPLAY, _)) => {
                if let Some(level) = self.progress.as_ref().and_then(|p| p.level_label(level)) {
                    match replays::suggested_name(&self.save_root, &self.sets[self.cur_set].id, &level) {
                        Ok(name) => {
                            self.name_entry = name;
                            self.name_from = Scr::Result;
                            self.go(Scr::ReplayName);
                        }
                        Err(e) => self.say(format!("Couldn’t choose a replay name: {e}")),
                    }
                }
            }
            Some((A_FINISH, _)) | Some((A_LEAVE, _)) => self.exit_level()?,
            Some((A_NEXT, _)) => {
                let start = self.progress.as_mut().ok_or("no set")?.play_next()?;
                self.begin_level(start, true)?;
            }
            Some((A_RETRY, _)) => self.retry()?,
            _ => {}
        }
        Ok(())
    }

    /// The ball keeps rolling behind a result screen, with no input.
    fn step_idle(&mut self, dt: f32) {
        if let Some(game) = &mut self.game {
            let mut events = Vec::new();
            game.advance(dt, &Controls::default(), |e| events.push(e));
            let radius = game.ball().radius;
            for e in events {
                self.audio.events(e, radius, self.orig_radius);
            }
        }
    }

    /// Leaves the level: a finished set shows its result, a challenge ends, a normal run returns.
    fn exit_level(&mut self) -> Result<()> {
        self.game = None;
        self.scene = None;
        self.capture = None;
        self.attempt = None;
        self.release_cursor();
        let Some(pr) = self.progress.as_mut() else {
            self.go(Scr::Title);
            return Ok(());
        };
        if pr.is_done() {
            let player = self.settings.player.clone();
            let rec = pr.finish_set(&player)?;
            if rec.needs_save {
                self.store_progress();
            }
            self.score_type = 0;
            self.go(Scr::Done);
        } else if pr.mode() == Mode::Challenge {
            self.go(Scr::Over);
        } else {
            pr.new_game(Mode::Normal);
            self.go(Scr::Levels);
        }
        Ok(())
    }

    fn over_screen(&mut self) -> Result<()> {
        self.backdrop();
        let mut tree = Tree::vstack();
        tree.label(0, "GAME OVER", Size::Lrg, g(col::GRY, col::RED));
        tree.layout(&self.ui, 0, 0);
        tree.draw(&mut self.ui);
        if self.since >= 3.0 || is_mouse_button_pressed(MouseButton::Left) || is_key_pressed(KeyCode::Escape) || is_key_pressed(KeyCode::Enter) {
            if let Some(pr) = &mut self.progress {
                pr.new_game(Mode::Normal);
            }
            self.go(Scr::Levels);
        }
        Ok(())
    }

    fn done_screen(&mut self) -> Result<()> {
        self.backdrop();
        let Some(pr) = self.progress.as_ref() else {
            self.go(Scr::Title);
            return Ok(());
        };
        let placement = pr.last_set_placement();
        let high = placement.is_high_score();
        let ty = self.score_type.min(1);
        let mut tree = Tree::vstack();
        if high {
            tree.label(0, "New Set Record", Size::Med, g(col::GRN, col::GRN));
        } else {
            tree.label(0, "Set Complete", Size::Med, g(col::BLU, col::GRN));
        }
        tree.space(0);
        let board = tree.hstack_in(0);
        tree.filler(board);
        let (table, hi) = if ty == 0 { (&pr.set_scores().coin, placement.coin) } else { (&pr.set_scores().time, placement.time) };
        score_box(&mut tree, board, SCORE_NAMES[ty], Some(table), hi.map(|r| r.index()));
        tree.filler(board);
        let types = tree.vstack_in(board);
        tree.filler(types);
        for k in 0..2 {
            let id = tree.state(types, SCORE_NAMES[k], Size::Sml, (A_SCORE, k as i32));
            tree.set_hilite(id, k == ty);
        }
        if high {
            tree.space(types);
            tree.state(types, "Change Name", Size::Sml, (A_CHANGE, 0));
        }
        tree.filler(types);
        tree.filler(board);
        tree.space(0);
        tree.start(0, "Select Level", Size::Sml, (A_BACK, 0));
        let act = self.pump(&mut tree, 0, 0);
        tree.draw(&mut self.ui);
        if is_key_pressed(KeyCode::Escape) {
            self.finish_done();
            return Ok(());
        }
        match act {
            Some((A_SCORE, k)) => self.score_type = k as usize,
            Some((A_CHANGE, _)) => self.open_name(Scr::Done),
            Some((A_BACK, _)) => self.finish_done(),
            _ => {}
        }
        Ok(())
    }

    fn finish_done(&mut self) {
        if let Some(pr) = &mut self.progress {
            pr.new_game(Mode::Normal);
        }
        self.score_type = 0;
        self.go(Scr::Levels);
    }

    // ---- player name ------------------------------------------------------------------------------------

    fn open_name(&mut self, from: Scr) {
        self.name_from = from;
        self.name_entry = self.settings.player.clone();
        self.go(Scr::Name);
    }

    fn enter_name_char(&mut self, ch: char) {
        let accepted = if self.screen == Scr::ReplayName {
            !ch.is_control() && self.name_entry.len() + ch.len_utf8() <= 251
        } else {
            ch.is_ascii_graphic() && self.name_entry.len() < NAME_MAX
        };
        if accepted {
            self.name_entry.push(ch);
        }
    }

    fn name_screen(&mut self) -> Result<()> {
        if matches!(self.name_from, Scr::Result) {
            self.draw_level(None);
        } else {
            self.backdrop();
        }
        while let Some(ch) = get_char_pressed() {
            self.enter_name_char(ch);
        }
        if is_key_pressed(KeyCode::Backspace) {
            self.name_entry.pop();
        }
        let mut tree = Tree::vstack();
        let replay = self.screen == Scr::ReplayName;
        tree.label(0, if replay { "Replay Name" } else { "Player Name" }, Size::Med, g(col::WHT, col::WHT));
        tree.space(0);
        let name = tree.label(0, &format!("{} ", self.name_entry), Size::Med, None);
        tree.set_rect(name, rect::ALL);
        tree.trunc_head(name);
        tree.space(0);
        let rows = [("0123456789", false), ("ABCDEFGHIJ", true), ("KLMNOPQRST", true)];
        for (chars, letters) in rows {
            let row = tree.harray(0);
            for ch in chars.chars() {
                let shown = if letters && !self.caps { ch.to_ascii_lowercase() } else { ch };
                tree.state(row, &shown.to_string(), Size::Sml, (A_KEY, shown as i32));
            }
        }
        let last = tree.harray(0);
        tree.state(last, if self.caps { "a" } else { "A" }, Size::Sml, (A_CAPS, 0));
        for ch in "UVWXYZ".chars() {
            let shown = if self.caps { ch } else { ch.to_ascii_lowercase() };
            tree.state(last, &shown.to_string(), Size::Sml, (A_KEY, shown as i32));
        }
        tree.state(last, "<", Size::Sml, (A_BKSP, 0));
        tree.space(0);
        let row = tree.harray(0);
        tree.start(row, if replay { "Save" } else { "OK" }, Size::Sml, (A_OK, 0));
        tree.state(row, "Cancel", Size::Sml, (A_CANCEL, 0));
        let act = self.pump(&mut tree, 0, 0);
        tree.draw(&mut self.ui);
        let back = self.name_from;
        if is_key_pressed(KeyCode::Escape) {
            self.screen_back(back);
            return Ok(());
        }
        let confirm = is_key_pressed(KeyCode::Enter) || is_key_pressed(KeyCode::KpEnter);
        match act {
            Some((A_KEY, c)) => {
                if let Some(ch) = char::from_u32(c as u32) {
                    self.enter_name_char(ch);
                }
            }
            Some((A_BKSP, _)) => {
                self.name_entry.pop();
            }
            Some((A_CAPS, _)) => self.caps = !self.caps,
            Some((A_CANCEL, _)) => self.screen_back(back),
            Some((A_OK, _)) => self.accept_name(back),
            _ if confirm => self.accept_name(back),
            _ => {}
        }
        Ok(())
    }

    fn screen_back(&mut self, back: Scr) {
        let keep = self.want_music.clone();
        self.go(back);
        self.want_music = keep;
    }

    fn accept_name(&mut self, back: Scr) {
        if self.screen == Scr::ReplayName {
            self.keep_replay(false);
            return;
        }
        if !valid_name(&self.name_entry) {
            return;
        }
        self.settings.player.clone_from(&self.name_entry);
        self.save_settings();
        if back == Scr::Title {
            self.go(Scr::Sets);
            return;
        }
        let target = match back {
            Scr::Done => Some(RenameTarget::SetOnly),
            Scr::Result => Some(RenameTarget::Level),
            _ => None,
        };
        if let (Some(target), Some(pr)) = (target, self.progress.as_mut()) {
            match pr.rename_player(&self.settings.player, target) {
                Ok(true) => self.store_progress(),
                Ok(false) => {}
                Err(e) => self.say(format!("The name was not changed in the scores: {e}")),
            }
        }
        self.screen_back(back);
    }

    fn keep_replay(&mut self, replace: bool) {
        match replays::name_last(&self.save_root, &self.name_entry, replace) {
            Ok(()) => {
                if let Some(attempt) = &mut self.attempt {
                    attempt.replay_ready = false;
                }
                self.screen_back(Scr::Result);
            }
            Err(e) if !replace && e.kind() == std::io::ErrorKind::AlreadyExists => self.go(Scr::OverwriteReplay),
            Err(e) => {
                self.go(Scr::ReplayName);
                self.say(format!("The replay wasn’t renamed: {e}"));
            }
        }
    }

    fn overwrite_replay_screen(&mut self) {
        self.draw_level(None);
        let mut tree = Tree::vstack();
        tree.label(0, "Overwrite?", Size::Med, g(col::RED, col::RED));
        tree.label(0, &self.name_entry, Size::Med, g(col::YEL, col::YEL));
        tree.space(0);
        let row = tree.harray(0);
        tree.start(row, "Cancel", Size::Sml, (A_CANCEL, 0));
        tree.state(row, "Overwrite", Size::Sml, (A_OK, 0));
        let act = self.pump(&mut tree, 0, 0);
        tree.draw(&mut self.ui);
        if is_key_pressed(KeyCode::Escape) || act == Some((A_CANCEL, 0)) {
            self.go(Scr::ReplayName);
        } else if act == Some((A_OK, 0)) {
            self.keep_replay(true);
        }
    }

    // ---- options ---------------------------------------------------------------------------------------

    fn options_screen(&mut self) -> Result<()> {
        self.backdrop();
        let mut tree = Tree::vstack();
        let head = tree.hstack_in(0);
        let title = tree.label(head, "Options", Size::Sml, None);
        tree.set_rect(title, rect::ALL);
        tree.filler(head);
        tree.start(head, "Back", Size::Sml, (A_BACK, 0));
        tree.space(0);
        let mouse_idx = ((600 - self.settings.mouse) / 50).clamp(0, 10);
        slider(&mut tree, "Mouse Sensitivity", A_MOUSE, mouse_idx);
        slider(&mut tree, "Sound Volume", A_SOUND, self.settings.sound);
        slider(&mut tree, "Music Volume", A_MUSIC, self.settings.music);
        let name = tree.hstack_in(0);
        tree.label(name, "Player Name", Size::Sml, g(col::WHT, col::WHT));
        tree.state(name, &self.settings.player, Size::Sml, (A_CHANGE, 0));
        let act = self.pump(&mut tree, 0, 0);
        tree.draw(&mut self.ui);
        if is_key_pressed(KeyCode::Escape) {
            self.go(Scr::Title);
            return Ok(());
        }
        match act {
            Some((A_BACK, _)) => self.go(Scr::Title),
            Some((A_MOUSE, i)) => {
                self.settings.mouse = 600 - 50 * i;
                self.save_settings();
            }
            Some((A_SOUND, i)) => {
                self.settings.sound = i;
                self.save_settings();
                self.audio.cue("select", 1.0);
            }
            Some((A_MUSIC, i)) => {
                self.settings.music = i;
                self.save_settings();
            }
            Some((A_CHANGE, _)) => self.open_name(Scr::Options),
            _ => {}
        }
        Ok(())
    }

    // ---- help -------------------------------------------------------------------------------------------

    fn help_screen(&mut self) -> Result<()> {
        self.backdrop();
        let mut tree = Tree::vstack();
        let menu = tree.harray(0);
        tree.start(menu, "Back", Size::Sml, (A_BACK, 0));
        for (n, name) in ["Rules", "Controls", "Modes", "Tricks"].iter().enumerate() {
            let id = tree.state(menu, name, Size::Sml, (A_PAGE, n as i32));
            tree.set_hilite(id, n == self.help_page);
        }
        tree.space(0);
        match self.help_page {
            0 => {
                tree.label(0, "Moving", Size::Sml, None);
                tree.multi(0, "Move the mouse or joystick\\or use keyboard arrows to\\tilt the floor causing the\\ball to roll.", Size::Sml, col::WHT, col::WHT);
                tree.space(0);
                tree.label(0, "Goal", Size::Sml, None);
                tree.multi(0, "Roll over coins to collect\\them.  Collect coins to\\unlock the goal and finish\\the level.", Size::Sml, col::WHT, col::WHT);
            }
            1 => {
                tree.multi(0, "Left and right mouse buttons rotate the view.\\Hold Shift for faster view rotation.", Size::Sml, col::WHT, col::WHT);
                tree.space(0);
                tree.multi(0, "Arrows: tilt    S / D: rotate the view\\Esc: Pause    R: Retry    1 / 2 / 3: Chase, Lazy, Manual Camera\\E or middle mouse button: toggle Manual Camera", Size::Sml, col::WHT, col::WHT);
            }
            2 => {
                tree.label(0, "Normal Mode", Size::Sml, None);
                tree.multi(0, "Finish a level before the time runs out.\\You need to collect coins in order to open the goal.", Size::Sml, col::WHT, col::WHT);
                tree.space(0);
                tree.label(0, "Challenge Mode", Size::Sml, None);
                tree.multi(0, "Start playing from the first level of the set.\\You start with only three balls, do not lose them.\\Earn an extra ball for each 100 coins collected.", Size::Sml, col::WHT, col::WHT);
            }
            _ => {
                tree.multi(0, "Corners can be used to jump.\\Get rolling and take aim\\at the angle. You may be able\\to reach new places.", Size::Sml, col::WHT, col::WHT);
                tree.space(0);
                tree.multi(0, "Tilting in 2 directions increases\\the slope. Use the manual camera\\and turn the camera by 45\\degrees for best results.", Size::Sml, col::WHT, col::WHT);
            }
        }
        let act = self.pump(&mut tree, 0, 0);
        tree.draw(&mut self.ui);
        if is_key_pressed(KeyCode::Escape) {
            self.go(Scr::Title);
            return Ok(());
        }
        match act {
            Some((A_BACK, _)) => self.go(Scr::Title),
            Some((A_PAGE, n)) => self.help_page = n as usize,
            _ => {}
        }
        Ok(())
    }
}

/// A plate holding a title and the three entries of a score table, with an optional highlighted rank.
fn score_box(tree: &mut Tree, parent: usize, title: &str, table: Option<&ScoreTable>, hi: Option<usize>) {
    let boxed = tree.vstack_in(parent);
    tree.set_rect(boxed, rect::ALL);
    let Some(table) = table else {
        tree.label(boxed, "Unavailable", Size::Sml, g(col::WHT, col::WHT));
        return;
    };
    tree.label(boxed, title, Size::Sml, g(col::WHT, col::WHT));
    for (i, entry) in table.entries().iter().enumerate() {
        let row = tree.hstack_in(boxed);
        tree.clock(row, entry.timer, Size::Sml);
        let name = tree.label(row, &format!("{:<8}", entry.player), Size::Sml, g(col::YEL, col::WHT));
        if hi == Some(i) {
            tree.set_colors(name, col::GRN, col::GRN);
        }
        tree.count(row, entry.coins, 1000, Size::Sml, g(col::YEL, col::WHT));
    }
}

/// An eleven-cell setting; cell `current` is highlighted and reports `(action, index)` when chosen.
fn slider(tree: &mut Tree, name: &str, action: u32, current: i32) {
    let row = tree.hstack_in(0);
    tree.label(row, name, Size::Sml, g(col::WHT, col::WHT));
    let cells = tree.harray(row);
    for i in 0..=10 {
        let id = tree.state(cells, " ", Size::Sml, (action, i));
        tree.set_hilite(id, i == current);
    }
}

#[cfg(test)]
mod tilt_tests {
    use super::*;

    #[test]
    fn released_arrow_levels_the_floor_at_the_original_keyboard_rate() {
        let mut input = TiltInput::default();
        input.sample([1, 0], [0.0; 2], 300.0);
        // Shipped Windows Hard 01 replay: update 279, immediately before Up is released.
        let mut floor = 19.994_413_f32;
        for _ in 0..6 {
            input.sample([0; 2], [0.0; 2], 300.0);
            floor += (input.angles[0] - floor) * (neverball::game::TICK / input.response);
        }
        assert!((floor - 15.221_039).abs() < 0.000_01, "floor at update 285: {floor}");
    }

    #[test]
    fn actual_mouse_motion_takes_over_without_losing_the_other_axis() {
        let mut input = TiltInput::default();
        input.sample([1, 0], [0.0; 2], 300.0);
        input.sample([1, 0], [0.0, 15.0], 300.0);
        input.sample([1, 0], [0.0; 2], 300.0);
        assert_eq!((input.angles, input.response), ([20.0, 2.0], MOUSE_RESPONSE));
        input.sample([0; 2], [0.0; 2], 300.0);
        assert_eq!((input.angles, input.response), ([0.0, 2.0], KEY_RESPONSE));
    }
}

#[cfg(test)]
mod player_tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn a_fresh_profile_has_no_player_until_a_name_is_saved() -> Result<()> {
        let stamp = SystemTime::now().duration_since(UNIX_EPOCH)?.as_nanos();
        let path = std::env::temp_dir().join(format!("nb-player-{}-{stamp}", std::process::id()));
        let mut settings = Settings::load(&path);
        assert_eq!(settings.player, "");
        settings.player = "Nora".into();
        settings.store(&path)?;
        let restored = Settings::load(&path);
        fs::remove_file(path)?;
        assert_eq!(restored.player, "Nora");
        Ok(())
    }
}
