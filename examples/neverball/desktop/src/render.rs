// SPDX-License-Identifier: GPL-2.0-or-later
//! Render the release's SOL geometry, billboards, textures and game objects.
mod gpu;
mod model;
use crate::{assets::{Assets,Result},frame::Frame};
use gpu::{Gpu,Lighting};
use model::Model;
use macroquad::prelude::*;
use neverball::{Level,sol::{Goal,ItemKind,Jump,Sol,Switch}};

pub(crate) struct Scene {
    floor:Model,sky:Option<Model>,back:Option<Model>,goals:Vec<Goal>,jumps:Vec<Jump>,switches:Vec<Switch>,
}
impl Scene {
    pub fn load(assets:&mut Assets,level:&Level)->Result<Self>{
        let sol=&level.sol;
        let sky=if let Some(name)=sol.attribute("grad").filter(|p|!p.is_empty()) {
            let dome=Sol::from_bytes(&assets.bytes("geom/back/back.sol")?)?;
            Some(Model::new(assets,&dome,Some(name))?)
        }else{None};
        let back=sol.attribute("back").filter(|p|!p.is_empty()).map(|p|Model::read(assets,p)).transpose()?;
        Ok(Self{floor:Model::new(assets,sol,None)?,sky,back,goals:sol.goals.clone(),jumps:sol.jumps.clone(),switches:sol.switches.clone()})
    }
}

pub(crate) struct Renderer {gpu:Gpu,ball:Model,coin:Model,five:Model,ten:Model,grow:Model,shrink:Model,goal:Model,jump:Model,beam:Model}
fn at(p:[f32;3],scale:Vec3)->Mat4{Mat4::from_scale_rotation_translation(scale,Quat::IDENTITY,p.into())}
fn body(frame:&dyn Frame,index:usize)->Mat4{let (p,[w,x,y,z])=frame.body_pose(index);Mat4::from_rotation_translation(Quat::from_xyzw(x,y,z,w),p.into())}
fn columns(b:[[f32;3];3])->Mat4{Mat4::from_mat3(Mat3::from_cols(b[0].into(),b[1].into(),b[2].into()))}
fn fixed(_:usize)->Mat4{Mat4::IDENTITY}
impl Renderer {
    pub fn new(assets:&mut Assets)->Result<Self>{Ok(Self{gpu:Gpu::new()?,ball:Model::read(assets,"ball/basic-ball/basic-ball-solid.sol")?,coin:Model::read(assets,"item/coin/coin.sol")?,five:Model::read(assets,"item/coin/coin5.sol")?,ten:Model::read(assets,"item/coin/coin10.sol")?,grow:Model::read(assets,"item/grow/grow.sol")?,shrink:Model::read(assets,"item/shrink/shrink.sol")?,goal:Model::read(assets,"geom/goal/goal.sol")?,jump:Model::read(assets,"geom/jump/jump.sol")?,beam:Model::read(assets,"geom/beam/beam.sol")?})}
    pub fn draw(&self,scene:&Scene,frame:&dyn Frame,view:Option<([f32;3],[f32;3])>,time:f32){
        let (eye,target)=view.unwrap_or_else(||frame.view());let eye=Vec3::from_array(eye);let target=Vec3::from_array(target);
        let fov=50.0*frame.jump_phase().map_or(1.0,|t|(2.0*(t-0.5).abs()).clamp(0.01,1.0));
        let camera=Camera3D{position:eye,target,up:Vec3::Y,fovy:fov.to_radians(),z_near:0.1,z_far:512.0,..Default::default()};
        clear_background(BLACK);set_camera(&camera);
        let ball=frame.ball();let p=Vec3::from_array(ball.position);
        let light=Lighting{eye,shadow:p.extend(ball.radius),unlit:false};let unlit=Lighting{unlit:true,..light};
        let one=Vec4::ONE;let origin=Mat4::from_translation(eye);
        if let Some(s)=&scene.sky{s.geometry(&self.gpu,origin*Mat4::from_scale(vec3(-256.0,256.0,-256.0)),fixed,unlit,one,true);}
        let back=(eye-target).normalize_or(Vec3::Z);let right=Vec3::Y.cross(back).normalize_or(Vec3::X);let facing=Mat4::from_mat3(Mat3::from_cols(right,back.cross(right),back));
        if let Some(b)=&scene.back{b.panels(&self.gpu,origin,facing,time,unlit,true);}
        let (axes,angles)=frame.tilt();let world=Mat4::from_translation(p)*Mat4::from_axis_angle(axes[1].into(),-angles[1].to_radians())*Mat4::from_axis_angle(axes[0].into(),-angles[0].to_radians())*Mat4::from_translation(-p);
        let local_facing=Mat4::from_mat3(Mat3::from_mat4(world).transpose())*facing;
        scene.floor.geometry(&self.gpu,world,|i|body(frame,i),light,one,false);
        let bm=world*Mat4::from_translation(p+vec3(0.0,0.001,0.0))*Mat4::from_scale(Vec3::splat(ball.radius))*columns(frame.basis());
        self.ball.geometry(&self.gpu,bm,fixed,light,one,false);
        for item in frame.items(){let model=match (item.kind,item.value){(ItemKind::Coin,n) if n>=10=>&self.ten,(ItemKind::Coin,n) if n>=5=>&self.five,(ItemKind::Coin,_)=>&self.coin,(ItemKind::Grow,_)=>&self.grow,(ItemKind::Shrink,_)=>&self.shrink,_=>continue};
            let m=world*at(item.position,Vec3::splat(0.15));model.panels(&self.gpu,m,local_facing,time,light,false);model.geometry(&self.gpu,m,fixed,light,one,false);
        }
        scene.floor.panels(&self.gpu,world,local_facing,time,light,false);
        let beam=|pos,r,h,color|self.beam.geometry(&self.gpu,world*at(pos,vec3(r,h,r)),fixed,unlit,color,false);
        if frame.goal_open(){for g in &scene.goals {beam(g.position,g.radius,3.0,vec4(1.0,1.0,0.0,0.5));self.goal.geometry(&self.gpu,world*at(g.position,vec3(g.radius,1.0,g.radius)),fixed,unlit,one,false);}}
        for j in &scene.jumps{beam(j.position,j.radius,2.0,vec4(0.7,0.5,1.0,if frame.jump_phase().is_some(){0.8}else{0.5}));self.jump.geometry(&self.gpu,world*at(j.position,vec3(j.radius,1.0,j.radius)),fixed,unlit,one,false);}
        for (i,s) in scene.switches.iter().enumerate(){if !s.invisible{let on=frame.switch_on(i);beam(s.position,s.radius,2.0,if on{vec4(0.0,1.0,0.0,0.5)}else{vec4(1.0,0.0,0.0,0.5)});}}
        gl_use_default_material();set_default_camera();
    }
}
