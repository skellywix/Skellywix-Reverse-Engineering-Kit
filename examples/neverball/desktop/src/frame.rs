// SPDX-License-Identifier: GPL-2.0-or-later
//! Values shared by the live camera and the replay renderer. This interface cannot advance a game.
use neverball::{game::Game, sol::{Ball,Item,Vec3}};

pub(crate) trait Frame {
    fn ball(&self) -> Ball;
    fn basis(&self) -> [Vec3; 3];
    fn view(&self) -> (Vec3,Vec3);
    fn tilt(&self) -> ([Vec3; 2],[f32; 2]);
    fn items(&self) -> &[Item];
    fn body_pose(&self,index:usize) -> (Vec3,[f32; 4]);
    fn switch_on(&self,index:usize) -> bool;
    fn goal_open(&self) -> bool;
    fn jump_phase(&self) -> Option<f32>;
}

impl Frame for Game {
    fn ball(&self) -> Ball { Game::ball(self) }
    fn basis(&self) -> [Vec3; 3] { self.motion().basis }
    fn view(&self) -> (Vec3,Vec3) { (self.camera().eye,self.camera().center) }
    fn tilt(&self) -> ([Vec3; 2],[f32; 2]) { self.floor_tilt() }
    fn items(&self) -> &[Item] { &self.level().sol.items }
    fn body_pose(&self,index:usize) -> (Vec3,[f32;4]) { self.motion().body_pose(&self.level().sol,index) }
    fn switch_on(&self,index:usize) -> bool { self.motion().switch_on(index).unwrap_or(false) }
    fn goal_open(&self) -> bool { Game::goal_open(self) }
    fn jump_phase(&self) -> Option<f32> { Game::jump_phase(self) }
}
