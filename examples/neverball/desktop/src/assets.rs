// SPDX-License-Identifier: GPL-2.0-or-later
//! The fixed Neverball release reads from its original, unmodified PK3.
use macroquad::prelude::{FilterMode, Texture2D};
use neverball::{Level, set::Set};
use std::{collections::HashMap, error::Error, fs::File, io::Read, path::Path};
use zip::ZipArchive;

pub(crate) type Result<T, E = Box<dyn Error>> = std::result::Result<T, E>;
const ENTRY_LIMIT: u64 = 8 * 1024 * 1024; // largest original member: 5,177,387 bytes

pub(crate) struct Assets {
    pack: ZipArchive<File>,
    textures: HashMap<String, Texture2D>,
}

impl Assets {
    pub fn open(path: &Path) -> Result<Self> {
        let file = File::open(path).map_err(|e| format!("Open {}: {e}", path.display()))?;
        Ok(Self { pack: ZipArchive::new(file)?, textures: HashMap::new() })
    }

    pub fn bytes(&mut self, name: &str) -> Result<Vec<u8>> {
        let entry = self.pack.by_name(name).map_err(|e| format!("Read {name} from the game pack: {e}"))?;
        let size = entry.size();
        if size > ENTRY_LIMIT || entry.is_dir() { return Err(format!("Invalid game asset: {name}").into()); }
        let mut data = Vec::with_capacity(size as usize);
        entry.take(size + 1).read_to_end(&mut data)?;
        if data.len() as u64 != size { return Err(format!("Wrong size for {name}").into()); }
        Ok(data)
    }

    pub fn level(&mut self, name: &str) -> Result<Level> {
        Ok(Level::from_bytes(name, &self.bytes(name)?)?)
    }

    pub fn sets(&mut self) -> Result<Vec<Set>> {
        let list = String::from_utf8(self.bytes("sets.txt")?)?;
        list.lines().filter(|s| !s.is_empty()).map(|name| {
            Ok(Set::parse(name, &String::from_utf8(self.bytes(name)?)?)?)
        }).collect()
    }

    pub fn contains(&self, name: &str) -> bool { self.pack.index_for_name(name).is_some() }

    pub fn texture(&mut self, name: &str) -> Result<Texture2D> {
        if let Some(texture) = self.textures.get(name) { return Ok(texture.clone()); }
        let pixels = image::load_from_memory(&self.bytes(name)?)?.into_rgba8();
        let (width, height) = pixels.dimensions();
        let texture = Texture2D::from_rgba8(width.try_into()?, height.try_into()?, pixels.as_raw());
        texture.set_filter(FilterMode::Linear);
        self.textures.insert(name.to_owned(), texture.clone());
        Ok(texture)
    }

    pub fn material_texture(&mut self, stem: &str) -> Result<Option<Texture2D>> {
        for prefix in ["textures/", ""] {
            for extension in ["png", "jpg"] {
                let name = format!("{prefix}{stem}.{extension}");
                if self.contains(&name) { return self.texture(&name).map(Some); }
            }
        }
        // These three materials also have no image in the original distribution.
        match stem { "default" | "mtrl/default" | "mtrl/dirt-tan" => Ok(None),
            _ => Err(format!("Missing material texture: {stem}").into()) }
    }
}
