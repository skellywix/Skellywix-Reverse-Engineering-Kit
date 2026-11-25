// SPDX-License-Identifier: GPL-2.0-or-later
//! SOL material passes for the desktop OpenGL renderer.
use crate::assets::Result;
use macroquad::{miniquad::{BlendFactor,BlendState,BlendValue,CullFace,Equation},prelude::*};
use neverball::sol::flag;
use super::model::Surface;

const VERT: &str = r#"#version 100
attribute vec3 position; attribute vec2 texcoord; attribute vec4 normal;
uniform mat4 Projection, Local; uniform vec3 Eye; uniform vec4 Shadow;
uniform vec4 Diffuse, Ambient, Specular, Emission, SurfaceMode, Tint;
varying highp vec2 uv; varying lowp vec4 shade; varying highp vec3 shadow_p;
void main(){
 vec4 p=Local*vec4(position,1.0); gl_Position=Projection*p;
 gl_Position.z-=SurfaceMode.w*0.00006*gl_Position.w;
 vec3 n=normalize(mat3(Local)*normal.xyz); vec3 v=normalize(Eye-p.xyz);
 vec3 l=normalize(vec3(-0.7,1.0,0.5)); vec3 l2=normalize(vec3(0.8,0.5,-0.6));
 float light=max(dot(n,l),0.0)*0.76+max(dot(n,l2),0.0)*0.32;
 float spec=pow(max(dot(n,normalize(l+v)),0.0),max(Specular.w,1.0));
 vec3 lit=Ambient.rgb*0.72+Diffuse.rgb*(0.32+light)+Emission.rgb+Specular.rgb*spec*0.55;
 shade=vec4(mix(lit,Diffuse.rgb,SurfaceMode.y),Diffuse.a)*Tint;
 vec3 reflected=reflect(-v,n); uv=mix(texcoord,reflected.xy*0.49+0.5,SurfaceMode.x);
 shadow_p=vec3((p.xz-Shadow.xz)/max(Shadow.w,0.0001),Shadow.y-p.y);
}"#;
const FRAG: &str = r#"#version 100
precision highp float;
uniform sampler2D Texture; uniform vec4 Sampling, SurfaceMode;
varying highp vec2 uv; varying lowp vec4 shade; varying highp vec3 shadow_p;
void main(){
 vec2 coord=mix(fract(uv),clamp(uv,0.0,1.0),Sampling.xy);coord.y=1.0-coord.y;
 vec4 color=texture2D(Texture,coord)*shade;
 if (Sampling.z>=0.0 && color.a<Sampling.z) discard;
 float shadow=(1.0-smoothstep(0.4,1.55,length(shadow_p.xy)))*(1.0-smoothstep(0.6,4.0,shadow_p.z))*step(-0.04,shadow_p.z);
 color.rgb*=1.0-SurfaceMode.z*shadow*0.55;
 gl_FragColor=color;
}"#;

#[derive(Clone,Copy)]
pub(super) struct Lighting {pub eye:Vec3,pub shadow:Vec4,pub unlit:bool}

pub(super) struct Gpu { pipes:Vec<Material> }
impl Gpu {
    pub fn new()->Result<Self> {
        let mut pipes=Vec::with_capacity(12);
        for mode in 0..12 {
            let pass=mode%3; let sky=mode/6!=0; let two_sided=(mode/3)%2!=0;
            let alpha=BlendFactor::Value(BlendValue::SourceAlpha);
            let params=PipelineParams{cull_face:if two_sided{CullFace::Nothing}else{CullFace::Back},
                depth_test:if sky{Comparison::Always}else{Comparison::LessOrEqual},depth_write:!sky && pass==0,
                color_blend:(pass!=0).then_some(BlendState::new(Equation::Add,alpha,if pass==2 {BlendFactor::One}else{BlendFactor::OneMinusValue(BlendValue::SourceAlpha)})),..Default::default()};
            let uniforms=[("Local",UniformType::Mat4),("Eye",UniformType::Float3),("Shadow",UniformType::Float4),("Diffuse",UniformType::Float4),("Ambient",UniformType::Float4),("Specular",UniformType::Float4),("Emission",UniformType::Float4),("SurfaceMode",UniformType::Float4),("Sampling",UniformType::Float4),("Tint",UniformType::Float4)].map(|(n,t)|UniformDesc::new(n,t)).into();
            pipes.push(load_material(ShaderSource::Glsl{vertex:VERT,fragment:FRAG},MaterialParams{pipeline_params:params,uniforms,..Default::default()})?);
        }
        Ok(Self{pipes})
    }
    pub fn draw(&self,mesh:&Mesh,s:&Surface,local:Mat4,light:Lighting,tint:Vec4,sky:bool){
        let bits=s.flags;
        let bit=|mask| if bits&mask!=0 {1.0}else{0.0};
        let pass=if bits&flag::MATERIAL_ADDITIVE!=0 {2}else if s.diffuse.w*tint.w<1.0 || sky || bits&flag::MATERIAL_TRANSPARENT!=0 {1}else{0};
        let double=sky || bits&flag::MATERIAL_TWO_SIDED!=0;
        let mat=&self.pipes[pass+3*usize::from(double)+6*usize::from(sky)];
        gl_use_material(mat);mat.set_uniform("Local",local);mat.set_uniform("Eye",light.eye);mat.set_uniform("Shadow",light.shadow);
        mat.set_uniform("Diffuse",s.diffuse);mat.set_uniform("Ambient",s.ambient);mat.set_uniform("Specular",s.specular);mat.set_uniform("Emission",s.emission);
        mat.set_uniform("SurfaceMode",vec4(bit(flag::MATERIAL_ENVIRONMENT),f32::from(u8::from(light.unlit)),bit(flag::MATERIAL_SHADOWED),bit(flag::MATERIAL_DECAL)));
        mat.set_uniform("Sampling",vec4(bit(flag::MATERIAL_CLAMP_S),bit(flag::MATERIAL_CLAMP_T),s.alpha_ref,0.0));mat.set_uniform("Tint",tint);draw_mesh(mesh);
    }
}
