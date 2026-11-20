use std::collections::{BTreeMap, HashSet};
use std::path::PathBuf;

use serde::Deserialize;

use crate::Error;

// the kinds a recipe may name
pub const KINDS: [&str; 5] = ["file", "agent-docs", "package-list", "lines", "lockfile"];

// the operating systems a recipe gives paths for
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Os {
    Linux,
    Windows,
    Macos,
}

impl Os {
    pub fn current() -> Self {
        if cfg!(target_os = "windows") {
            Os::Windows
        } else if cfg!(target_os = "macos") {
            Os::Macos
        } else {
            Os::Linux
        }
    }
}

// one item as written in the recipes file
#[derive(Clone, Debug, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct Recipe {
    pub name: String,
    pub kind: String,
    pub linux: Option<String>,
    pub windows: Option<String>,
    pub macos: Option<String>,
    #[serde(default)]
    pub ignore: Vec<String>,
    #[serde(default)]
    pub devices: Vec<String>,
    pub mode: Option<String>,
    pub glob: Option<String>,
    pub run: Option<String>,
    pub target: Option<String>,
    #[serde(default)]
    pub capture: BTreeMap<String, String>,
}

// an item ready for this node with its id, kind and local path
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Item {
    pub id: String,
    pub kind: String,
    pub path: Option<PathBuf>,
    pub ignore: Vec<String>,
    pub mode: Option<u32>,
    pub recipe: Recipe,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct File {
    #[serde(default)]
    item: Vec<Recipe>,
}

// every recipe in a recipes file, with names unique and kinds known
pub fn parse(text: &str) -> Result<Vec<Recipe>, Error> {
    let file: File = toml::from_str(text).map_err(|e| Error::Recipe(e.to_string()))?;
    let mut seen = HashSet::new();
    for recipe in &file.item {
        if recipe.name.trim().is_empty() {
            return Err(Error::Recipe("an item has no name".into()));
        }
        if !KINDS.contains(&recipe.kind.as_str()) {
            return Err(Error::Recipe(format!(
                "item {} has unknown kind {}",
                recipe.name, recipe.kind
            )));
        }
        if !seen.insert(&recipe.name) {
            return Err(Error::Recipe(format!("item {} appears twice", recipe.name)));
        }
    }
    Ok(file.item)
}

impl Recipe {
    // the item for this os and device, or the reason it does not apply here
    pub fn resolve(
        &self,
        os: Os,
        device: &str,
        env: &dyn Fn(&str) -> Option<String>,
    ) -> Result<Item, String> {
        if !self.devices.is_empty() && !self.devices.iter().any(|d| d == device) {
            return Err(format!("{} is not for device {device}", self.name));
        }
        let raw = match os {
            Os::Linux => &self.linux,
            Os::Windows => &self.windows,
            Os::Macos => &self.macos,
        };
        let path = raw.as_deref().map(|p| expand(p, os, env)).transpose()?;
        if path.is_none() && matches!(self.kind.as_str(), "file" | "lines") {
            return Err(format!("{} has no path for {os:?}", self.name));
        }
        let mode = self
            .mode
            .as_deref()
            .map(|m| {
                u32::from_str_radix(m, 8).map_err(|_| format!("{} has bad mode {m}", self.name))
            })
            .transpose()?;
        Ok(Item {
            id: format!("{}:{}", self.kind, self.name),
            kind: self.kind.clone(),
            path,
            ignore: self.ignore.clone(),
            mode,
            recipe: self.clone(),
        })
    }
}

// expand a leading tilde and environment variables written as $NAME, ${NAME} or %NAME%
fn expand(raw: &str, os: Os, env: &dyn Fn(&str) -> Option<String>) -> Result<PathBuf, String> {
    let lookup = |name: &str| env(name).ok_or_else(|| format!("{name} is not set"));
    let mut out = String::new();
    let mut rest = raw;
    let home = if os == Os::Windows {
        "USERPROFILE"
    } else {
        "HOME"
    };
    if let Some(after) = rest.strip_prefix('~')
        && (after.is_empty() || after.starts_with(['/', '\\']))
    {
        out.push_str(&lookup(home)?);
        rest = after;
    }
    while let Some(start) = rest.find(['$', '%']) {
        out.push_str(&rest[..start]);
        let after = &rest[start + 1..];
        let (name, next) = if rest[start..].starts_with("${") {
            let end = after.find('}').ok_or(format!("unclosed ${{ in {raw}"))?;
            (&after[1..end], &after[end + 1..])
        } else if rest[start..].starts_with('%') {
            let end = after.find('%').ok_or(format!("unclosed % in {raw}"))?;
            (&after[..end], &after[end + 1..])
        } else {
            let end = after
                .find(|c: char| !(c.is_ascii_alphanumeric() || c == '_'))
                .unwrap_or(after.len());
            (&after[..end], &after[end..])
        };
        if name.is_empty() {
            return Err(format!("empty variable in {raw}"));
        }
        out.push_str(&lookup(name)?);
        rest = next;
    }
    out.push_str(rest);
    Ok(PathBuf::from(out))
}

#[cfg(test)]
mod tests {
    use super::*;

    const SAMPLE: &str = r#"
[[item]]
name = "zed-config"
kind = "file"
linux = "~/.config/zed"
windows = "%APPDATA%/Zed"
ignore = ["*.log"]

[[item]]
name = "zed-extensions"
kind = "package-list"
capture.linux = "ls ~/.local/share/zed/extensions/installed"
target = "zed-config/settings.json#auto_install_extensions"

[[item]]
name = "ssh-authorized-keys"
kind = "lines"
linux = "~/.ssh/authorized_keys"
mode = "600"
devices = ["pc", "laptop"]

[[item]]
name = "lockfiles"
kind = "lockfile"
glob = "~/projects/**/package-lock.json"
run = "npm ci"
"#;

    fn env(name: &str) -> Option<String> {
        match name {
            "HOME" => Some("/home/t".into()),
            "APPDATA" => Some("C:/Users/t/AppData/Roaming".into()),
            _ => None,
        }
    }

    #[test]
    fn the_spec_sample_parses_and_resolves_on_linux() {
        let recipes = parse(SAMPLE).unwrap();
        assert_eq!(recipes.len(), 4);
        let zed = recipes[0].resolve(Os::Linux, "pc", &env).unwrap();
        assert_eq!(zed.id, "file:zed-config");
        assert_eq!(zed.path, Some(PathBuf::from("/home/t/.config/zed")));
        assert_eq!(zed.ignore, vec!["*.log"]);
        let keys = recipes[2].resolve(Os::Linux, "laptop", &env).unwrap();
        assert_eq!(keys.mode, Some(0o600));
        assert_eq!(
            keys.path,
            Some(PathBuf::from("/home/t/.ssh/authorized_keys"))
        );
        let extensions = recipes[1].resolve(Os::Linux, "pc", &env).unwrap();
        assert_eq!(extensions.path, None);
        assert_eq!(
            extensions.recipe.capture.get("linux").map(String::as_str),
            Some("ls ~/.local/share/zed/extensions/installed")
        );
        let lockfiles = recipes[3].resolve(Os::Linux, "pc", &env).unwrap();
        assert_eq!(lockfiles.recipe.run.as_deref(), Some("npm ci"));
    }

    #[test]
    fn windows_paths_expand_percent_variables() {
        let recipes = parse(SAMPLE).unwrap();
        let zed = recipes[0].resolve(Os::Windows, "pc", &env).unwrap();
        assert_eq!(
            zed.path,
            Some(PathBuf::from("C:/Users/t/AppData/Roaming/Zed"))
        );
    }

    #[test]
    fn items_not_for_here_are_skipped_with_a_reason() {
        let recipes = parse(SAMPLE).unwrap();
        let other_device = recipes[2].resolve(Os::Linux, "webdev", &env).unwrap_err();
        assert!(other_device.contains("not for device webdev"));
        let no_path = recipes[2].resolve(Os::Macos, "pc", &env).unwrap_err();
        assert!(no_path.contains("no path for Macos"));
        let unset = parse("[[item]]\nname = \"x\"\nkind = \"file\"\nlinux = \"$NOPE/x\"\n")
            .unwrap()[0]
            .resolve(Os::Linux, "pc", &env)
            .unwrap_err();
        assert!(unset.contains("NOPE is not set"));
    }

    #[test]
    fn bad_recipes_are_clear_errors_naming_the_item() {
        let unknown = parse("[[item]]\nname = \"x\"\nkind = \"photos\"\n").unwrap_err();
        assert_eq!(
            unknown,
            Error::Recipe("item x has unknown kind photos".into())
        );
        assert!(parse("[[item]]\nkind = \"file\"\n").is_err());
        assert!(parse("[[item]]\nname = \" \"\nkind = \"file\"\n").is_err());
        let twice = parse(
            "[[item]]\nname = \"x\"\nkind = \"file\"\n[[item]]\nname = \"x\"\nkind = \"lines\"\n",
        );
        assert_eq!(
            twice.unwrap_err(),
            Error::Recipe("item x appears twice".into())
        );
        assert!(parse("[[item]]\nname = \"x\"\nkind = \"file\"\ncolour = \"red\"\n").is_err());
    }

    #[test]
    fn expansion_handles_each_variable_form() {
        let env = |n: &str| (n == "A").then(|| "/a".to_string());
        assert_eq!(expand("$A/b", Os::Linux, &env), Ok(PathBuf::from("/a/b")));
        assert_eq!(expand("${A}b", Os::Linux, &env), Ok(PathBuf::from("/ab")));
        assert_eq!(
            expand("%A%/b", Os::Windows, &env),
            Ok(PathBuf::from("/a/b"))
        );
        assert_eq!(
            expand("~user/x", Os::Linux, &env),
            Ok(PathBuf::from("~user/x"))
        );
        assert!(expand("${A", Os::Linux, &env).is_err());
    }
}
