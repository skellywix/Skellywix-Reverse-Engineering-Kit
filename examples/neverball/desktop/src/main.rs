// SPDX-License-Identifier: GPL-2.0-or-later
#![cfg_attr(target_os = "windows", windows_subsystem = "windows")]
mod assets;
mod audio;
mod demo_codec;
mod frame;
mod render;
mod replays;
mod shell;
mod shell_ui;

use macroquad::prelude::*;
use std::{env,fs,path::PathBuf};

fn window_conf()->Conf { Conf{window_title:"Neverball".into(),window_width:800,window_height:600,window_resizable:true,..Default::default()} }
fn paths()->assets::Result<(PathBuf,PathBuf)>{
    let exe=env::current_exe()?;
    let pack=exe.parent().ok_or("Game executable has no parent folder")?.join("data/data-1.6.0.pk3");
    #[cfg(windows)] let home=env::var_os("APPDATA").map(PathBuf::from).ok_or("Windows did not provide a user data folder")?;
    #[cfg(not(windows))] let home=env::var_os("XDG_DATA_HOME").map(PathBuf::from).or_else(||env::var_os("HOME").map(|h|PathBuf::from(h).join(".local/share"))).ok_or("No user data folder")?;
    // Do not overwrite scores from the original while the two builds are compared.
    let saves=home.join("Neverball Rust");fs::create_dir_all(&saves)?;Ok((pack,saves))
}
async fn play()->assets::Result<()> {let (pack,saves)=paths()?;shell::run(assets::Assets::open(&pack)?,&saves).await}
#[macroquad::main(window_conf)]
async fn main(){
    if let Err(error)=play().await {
        let detail=error.to_string();eprintln!("Neverball: {detail}");
        let lines:Vec<_>=detail.split_inclusive(' ').collect::<Vec<_>>().chunks(7).map(|words|words.concat()).collect();
        loop {clear_background(BLACK);draw_text("Neverball couldn't start or continue.",24.0,58.0,28.0,WHITE);
            for (i,line) in lines.iter().enumerate(){draw_text(line,24.0,100.0+i as f32*24.0,20.0,WHITE);}
            draw_text("Press Esc or close the window to exit.",24.0,screen_height()-28.0,20.0,LIGHTGRAY);
            if is_key_pressed(KeyCode::Escape)||is_quit_requested(){return;}next_frame().await;
        }
    }
}
