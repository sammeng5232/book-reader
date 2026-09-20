// The TeX engine Book Reader ships on Android.
//
// Tectonic (XeTeX + xdvipdfmx) compiled for the device, driven through JNI.  It
// never touches the network: the LaTeX packages come from a directory bundle that
// the app unpacks from its assets, and the format file (`latex.fmt`) is built once
// on the device and cached.
//
// Build: ../build-native.sh   (cargo-ndk + the vcpkg C stack; see that script)

use std::fmt::Arguments;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use jni::objects::{JObject, JString};
use jni::sys::jstring;
use jni::JNIEnv;
use tectonic::driver::{OutputFormat, PassSetting, ProcessingSessionBuilder};
use tectonic_bundles::dir::DirBundle;
use tectonic_errors::Error;
use tectonic_status_base::{MessageKind, StatusBackend};

/// Collects what the engine says, so the app can show it when something fails.
#[derive(Default)]
struct Collected {
    lines: Vec<String>,
    log: String,
}

#[derive(Default)]
struct CollectingStatus(Mutex<Collected>);

impl CollectingStatus {
    fn take(&self) -> (Vec<String>, String) {
        let mut guard = self.0.lock().unwrap_or_else(|e| e.into_inner());
        (std::mem::take(&mut guard.lines), std::mem::take(&mut guard.log))
    }
}

impl StatusBackend for CollectingStatus {
    fn report(&mut self, kind: MessageKind, args: Arguments, err: Option<&Error>) {
        if matches!(kind, MessageKind::Note) {
            return;
        }
        let mut text = format!("{kind:?}: {args}");
        if let Some(e) = err {
            text.push_str(&format!("\n  caused by: {e}"));
            for cause in e.chain().skip(1) {
                text.push_str(&format!("\n  because: {cause}"));
            }
        }
        let mut guard = self.0.lock().unwrap_or_else(|e| e.into_inner());
        guard.lines.push(text);
    }

    fn dump_error_logs(&mut self, output: &[u8]) {
        let text = String::from_utf8_lossy(output);
        let mut guard = self.0.lock().unwrap_or_else(|e| e.into_inner());
        guard.log.push_str(&text);
    }
}

fn typeset(tex: &Path, pdf: &Path, bundle: &Path, cache: &Path) -> Result<String, String> {
    let name = tex
        .file_name()
        .and_then(|n| n.to_str())
        .ok_or_else(|| "the document has no file name".to_string())?;
    let out_dir: PathBuf = pdf
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(|| PathBuf::from("."));
    std::fs::create_dir_all(&out_dir).map_err(|e| format!("cannot write to the output folder: {e}"))?;
    std::fs::create_dir_all(cache).map_err(|e| format!("cannot write to the cache folder: {e}"))?;

    let mut status = CollectingStatus::default();
    let mut builder = ProcessingSessionBuilder::default();
    builder
        .bundle(Box::new(DirBundle::new(bundle)))
        .primary_input_path(tex)
        .tex_input_name(name)
        .filesystem_root(tex.parent().unwrap_or(Path::new(".")))
        .format_name("latex")
        .format_cache_path(cache)
        .output_format(OutputFormat::Pdf)
        .output_dir(&out_dir)
        .keep_intermediates(false)
        .keep_logs(false)
        .pass(PassSetting::Default)
        .print_stdout(false);

    let outcome = (|| -> Result<(), Error> {
        let mut session = builder.create(&mut status)?;
        session.run(&mut status)?;
        Ok(())
    })();

    let (messages, log) = status.take();
    if let Err(e) = outcome {
        let mut text = format!("{e}");
        if !messages.is_empty() {
            text.push('\n');
            text.push_str(&messages.join("\n"));
        }
        if !log.is_empty() {
            text.push_str("\n--- TeX log ---\n");
            let tail: String = log.chars().rev().take(4000).collect::<Vec<_>>().into_iter().rev().collect();
            text.push_str(&tail);
        }
        return Err(text);
    }

    // Tectonic names the PDF after the input; move it if the caller wanted another name.
    let produced = out_dir.join(Path::new(name).with_extension("pdf"));
    if produced != pdf && produced.is_file() {
        std::fs::rename(&produced, pdf).map_err(|e| format!("cannot rename the PDF: {e}"))?;
    }
    if !pdf.is_file() {
        return Err("the engine finished but wrote no PDF".to_string());
    }
    Ok(String::new())
}

fn jstr(env: &mut JNIEnv, s: &JString) -> Result<String, String> {
    env.get_string(s)
        .map(|v| v.into())
        .map_err(|e| format!("bad string from Java: {e}"))
}

/// `TexEngine.version()`
#[no_mangle]
pub extern "system" fn Java_com_bookreader_TexEngine_version<'local>(
    env: JNIEnv<'local>,
    _this: JObject<'local>,
) -> jstring {
    let text = format!(
        "Tectonic {} (XeTeX), {}",
        tectonic::FORMAT_SERIAL,
        std::env::consts::ARCH
    );
    env.new_string(text)
        .map(|s| s.into_raw())
        .unwrap_or(std::ptr::null_mut())
}

/// `TexEngine.typeset(texPath, pdfPath, bundlePath, cachePath)` -> "" or an error report.
#[no_mangle]
pub extern "system" fn Java_com_bookreader_TexEngine_typeset<'local>(
    mut env: JNIEnv<'local>,
    _this: JObject<'local>,
    tex: JString<'local>,
    pdf: JString<'local>,
    bundle: JString<'local>,
    cache: JString<'local>,
) -> jstring {
    let args = (|| -> Result<(String, String, String, String), String> {
        Ok((
            jstr(&mut env, &tex)?,
            jstr(&mut env, &pdf)?,
            jstr(&mut env, &bundle)?,
            jstr(&mut env, &cache)?,
        ))
    })();
    let message = match args {
        Err(e) => e,
        Ok((tex, pdf, bundle, cache)) => {
            // A panic must never cross back into the JVM.
            match catch_unwind(AssertUnwindSafe(|| {
                typeset(Path::new(&tex), Path::new(&pdf), Path::new(&bundle), Path::new(&cache))
            })) {
                Ok(Ok(_)) => String::new(),
                Ok(Err(e)) => e,
                Err(_) => "the TeX engine stopped unexpectedly".to_string(),
            }
        }
    };
    env.new_string(message)
        .map(|s| s.into_raw())
        .unwrap_or(std::ptr::null_mut())
}
