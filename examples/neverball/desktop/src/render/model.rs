// SPDX-License-Identifier: GPL-2.0-or-later
// Geometry and pass order from Neverball 1.6.0 share/solid_draw.c.
use super::gpu::{Gpu,Lighting};
use crate::assets::{Assets,Result};
use macroquad::prelude::*;
use neverball::sol::{flag,Billboard,Material as SolMaterial,Sol};
use std::collections::HashMap;

pub(super) struct Surface {pub diffuse:Vec4,pub ambient:Vec4,pub specular:Vec4,pub emission:Vec4,pub flags:u32,pub alpha_ref:f32,texture:Option<Texture2D>}
impl Surface {
    fn new(s:&SolMaterial,texture:Option<Texture2D>)->Self {
        Self{diffuse:s.diffuse.into(),ambient:s.ambient.into(),specular:vec4(s.specular[0],s.specular[1],s.specular[2],s.shininess),emission:s.emission.into(),flags:s.flags,
            alpha_ref:if s.has(flag::MATERIAL_ALPHA_TEST){s.alpha_ref}else{-1.0},texture}
    }
    fn order(&self)->u8 {let alpha=self.flags&(flag::MATERIAL_TRANSPARENT|flag::MATERIAL_ADDITIVE)!=0;let decal=self.flags&flag::MATERIAL_DECAL!=0;match (alpha,decal){(false,false)=>0,(false,true)=>1,(true,true)=>2,(true,false)=>3}}
}
struct Part {body:usize,surface:usize,mesh:Mesh}
struct Panel {data:Billboard,mesh:Mesh}
pub(super) struct Model {surfaces:Vec<Surface>,parts:Vec<Part>,panels:Vec<Panel>}

impl Model {
    pub fn read(assets:&mut Assets,name:&str)->Result<Self>{let s=Sol::from_bytes(&assets.bytes(name)?)?;Self::new(assets,&s,None)}
    pub fn new(assets:&mut Assets,s:&Sol,override_texture:Option<&str>)->Result<Self>{
        let mut surfaces=Vec::with_capacity(s.materials.len());
        for m in &s.materials {let tex=if let Some(path)=override_texture {Some(assets.texture(path)?)} else {assets.material_texture(&m.texture)?};surfaces.push(Surface::new(m,tex));}
        let mut parts=Vec::new();
        for (body,b) in s.bodies.iter().enumerate(){
            let mut groups=vec![Vec::new();surfaces.len()];
            for &i in s.lumps[b.lumps.clone()].iter().flat_map(|l|&l.geoms).chain(b.geoms.iter()) {
                let g=&s.geoms[i];groups[g.material].push(g.offsets);
            }
            for (surface,tris) in groups.into_iter().enumerate(){
                // macroquad's default batch accepts < 5,000 indices; a corner maps to at most one vertex per batch.
                for chunk in tris.chunks(1600) {
                    let mut indices=Vec::with_capacity(chunk.len()*3);let mut vertices=Vec::new();let mut mapped=HashMap::new();
                    for &oi in chunk.iter().flatten(){
                        let i=*mapped.entry(oi).or_insert_with(||{
                            let offset=s.offsets[oi];let pos=s.vertices[offset.vertex];let uv=s.tex_coords[offset.tex_coord];
                            let mut v=Vertex::new2(pos.into(),uv.into(),WHITE);v.normal=Vec3::from_array(s.sides[offset.side].normal).extend(0.0);
                            let index=vertices.len() as u16;vertices.push(v);index
                        }); indices.push(i);
                    }
                    parts.push(Part{body,surface,mesh:Mesh{indices,vertices,texture:surfaces[surface].texture.clone()}});
                }
            }
        }
        parts.sort_by_key(|p|surfaces[p.surface].order());
        let panels=s.billboards.iter().map(|b|{let edge=b.flags&flag::BILLBOARD_EDGE!=0;let bottom=if edge{0.0}else{-0.5};let vertices=[(-0.5,bottom,0.0,0.0),(0.5,bottom,1.0,0.0),(-0.5,bottom+1.0,0.0,1.0),(0.5,bottom+1.0,1.0,1.0)].into_iter().map(|(x,y,u,v)|{let mut p=Vertex::new(x,y,0.0,u,v,WHITE);p.normal=Vec3::Z.extend(0.0);p}).collect();Panel{data:*b,mesh:Mesh{indices:vec![0,1,2,2,1,3],vertices,texture:surfaces[b.material].texture.clone()}}}).collect();
        Ok(Self{surfaces,parts,panels})
    }
    pub fn geometry(&self,gpu:&Gpu,base:Mat4,body:impl Fn(usize)->Mat4,light:Lighting,tint:Vec4,sky:bool){
        let mut last=None;
        for part in &self.parts{let matrix=match last {Some((id,m)) if id==part.body=>m,_=>{let m=base*body(part.body);last=Some((part.body,m));m}};gpu.draw(&part.mesh,&self.surfaces[part.surface],matrix,light,tint,sky);}
    }
    pub fn panels(&self,gpu:&Gpu,base:Mat4,facing:Mat4,time:f32,light:Lighting,sky:bool){
        let mut unlit=light;unlit.unlit=true;
        for panel in &self.panels{
            let b=panel.data;if sky && !(0.0..512.0).contains(&b.distance){continue;}
            let t=if sky {if b.repeat_time>0.0{time.rem_euclid(b.repeat_time)-b.repeat_time*0.5}else{0.0}}else{b.repeat_time*time};
            let wave=if sky{t*t}else{t.sin()};let coef=|v:[f32;3]|v[0]+v[1]*t+v[2]*wave;
            let (w,h)=(coef(b.width),coef(b.height));if w<=0.0||h<=0.0{continue;}
            let (x,y,z)=(coef(b.rot_x).to_radians(),coef(b.rot_y).to_radians(),coef(b.rot_z).to_radians());
            let mut m=if sky{let mut m=base*Mat4::from_rotation_y(y)*Mat4::from_rotation_x(x)*Mat4::from_translation(vec3(0.0,0.0,-b.distance));
                if b.flags&flag::BILLBOARD_FLAT!=0{m=m*Mat4::from_rotation_x(-x-std::f32::consts::FRAC_PI_2)*Mat4::from_rotation_y(-y);}
                if b.flags&flag::BILLBOARD_EDGE!=0{m*=Mat4::from_rotation_x(-x);}m
            }else{let mut m=base*Mat4::from_translation(b.position.into());if b.flags&flag::BILLBOARD_NOFACE==0{m*=facing;}m*Mat4::from_rotation_x(x)*Mat4::from_rotation_y(y)};
            m=m*Mat4::from_rotation_z(z)*Mat4::from_scale(vec3(w,h,1.0));gpu.draw(&panel.mesh,&self.surfaces[b.material],m,unlit,Vec4::ONE,sky);
        }
    }
}
