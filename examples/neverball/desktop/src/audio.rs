// SPDX-License-Identifier: GPL-2.0-or-later
//! Play the release's Ogg recordings, not synthesized substitutes.
use crate::assets::{Assets,Result};
use macroquad::audio::{Sound, PlaySoundParams, load_sound_from_bytes, play_sound, stop_sound, set_sound_volume};
use neverball::{game::{Events,Outcome},sol::ItemKind};
use std::collections::HashMap;

const CUES: &[&str] = &["menu","select","ready","set","go","ball","bumplil","bump","bumpbig","coin","tick","tock","switch","jump","goal","time","fall","record","over","grow","shrink","success"];

pub(crate) struct Audio {
    cues: HashMap<&'static str,Sound>, song: Option<(String,Sound)>, effects_volume:f32, music_volume:f32,
}
impl Audio {
    pub async fn new(assets:&mut Assets) -> Result<Self> {
        let mut cues=HashMap::with_capacity(CUES.len());
        for &name in CUES {
            let bytes=assets.bytes(&format!("snd/{name}.ogg"))?;
            let sound=load_sound_from_bytes(&bytes).await?;
            cues.insert(name,sound);
        }
        Ok(Self { cues,song:None,effects_volume:1.0,music_volume:0.6 })
    }
    pub fn volumes(&mut self,sound:f32,music:f32) {
        self.effects_volume=sound.clamp(0.0,1.0);self.music_volume=music.clamp(0.0,1.0);
        if let Some((_,sound))=&self.song {set_sound_volume(sound,self.music_volume);}
    }
    pub fn cue(&self,name:&str,gain:f32) {
        let name=name.strip_prefix("snd/").unwrap_or(name);
        let stem=name.strip_suffix(".ogg").unwrap_or(name);
        if let Some(sound)=self.cues.get(stem) {
            play_sound(sound,PlaySoundParams{looped:false,volume:(self.effects_volume*gain).clamp(0.0,1.0)});
        }
    }
    pub async fn music(&mut self,assets:&mut Assets,path:&str) -> Result<()> {
        if self.song.as_ref().is_some_and(|(name,_)|name==path) {return Ok(());}
        let next=if path.is_empty(){None}else{Some((path.to_owned(),load_sound_from_bytes(&assets.bytes(path)?).await?))};
        if let Some((_,music))=self.song.take() {stop_sound(&music);}
        if let Some((_,music))=&next { play_sound(music,PlaySoundParams{looped:true,volume:self.music_volume}); }
        self.song=next;Ok(())
    }
    pub fn events(&self,events:Events,radius:f32,original_radius:f32) {
        if events.impact>0.5 {let name=if radius>original_radius{"bumpbig"}else if radius<original_radius{"bumplil"}else{"bump"};self.cue(name,(events.impact-0.5)*2.0);}
        if events.pickup.is_some_and(|i|i.kind==ItemKind::Coin){self.cue("coin",1.0);}
        match events.resize {Some(ItemKind::Grow)=>self.cue("grow",1.0),Some(ItemKind::Shrink)=>self.cue("shrink",1.0),_=>{}}
        if events.switched {self.cue("switch",1.0);}
        if events.jumped {self.cue("jump",1.0);}
        if let Some(o)=events.outcome { self.cue(match o {Outcome::Goal=>"goal",Outcome::Fall=>"fall",Outcome::Time=>"time"},1.0); }
        if events.opened_goal {self.cue("switch",1.0);}
    }
}
