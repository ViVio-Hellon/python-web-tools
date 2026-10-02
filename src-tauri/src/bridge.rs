//! Python(`bridge.py`)を子として起動し、標準入出力で要求を渡す。
//!
//! やりとりの形は `bridge.py` の説明と同じ:
//!
//! ```text
//! 要求  {"id": 7, "method": "POST", "path": "/api/x", "query": "", "headers": [[k, v]], "len": 12}\n<本文>
//! 応答  {"id": 7, "status": 200, "headers": [[k, v]], "len": 345}\n<本文>
//! 知らせ {"event": "started" | "quit" | "fatal", ...}\n
//! ```
//!
//! **ポートは使わない。** 画面(WebView)からの要求は、Tauri の独自の宛先
//! (`app://`)で受けて、ここから Python へ渡す。

use std::collections::{HashMap, VecDeque};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Condvar, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

/// 標準エラーの末尾を何行覚えておくか(起動に失敗したとき画面に出す)
const STDERR_LINES: usize = 60;

/// 1件の応答を待つ上限。取り込みなどの長い処理は背景で走るので、
/// 1件がこれほど掛かることは無い。止まったまま待たせきりにしないための上限
const REPLY_TIMEOUT: Duration = Duration::from_secs(600);

pub struct Reply {
    pub status: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

/// 起動できなかった理由。画面にそのまま出す
#[derive(Clone, Debug)]
pub struct Failure {
    pub message: String,
    pub hint: String,
    pub log_dir: String,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Phase {
    /// まだ Python を起動していない / 起動待ち
    Starting,
    /// 要求を受け付けている
    Started,
    /// 終わった(終了してよいと言われた・落ちた)
    Ended,
}

struct State {
    phase: Phase,
    failure: Option<Failure>,
    /// どの Python で動いているか(画面の診断用)
    python: String,
}

type Pending = Arc<Mutex<HashMap<u64, mpsc::Sender<Reply>>>>;

pub struct Bridge {
    root: PathBuf,
    token: String,
    state: Mutex<State>,
    changed: Condvar,
    stdin: Mutex<Option<ChildStdin>>,
    child: Mutex<Option<Child>>,
    pending: Pending,
    next_id: AtomicU64,
    stderr: Arc<Mutex<VecDeque<String>>>,
    on_quit: Mutex<Option<Box<dyn Fn() + Send + Sync>>>,
    on_lost: Mutex<Option<Box<dyn Fn() + Send + Sync>>>,
}

impl Bridge {
    pub fn new(root: PathBuf, token: String) -> Arc<Self> {
        Arc::new(Self {
            root,
            token,
            state: Mutex::new(State {
                phase: Phase::Starting,
                failure: None,
                python: String::new(),
            }),
            changed: Condvar::new(),
            stdin: Mutex::new(None),
            child: Mutex::new(None),
            pending: Arc::new(Mutex::new(HashMap::new())),
            next_id: AtomicU64::new(1),
            stderr: Arc::new(Mutex::new(VecDeque::new())),
            on_quit: Mutex::new(None),
            on_lost: Mutex::new(None),
        })
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    /// 「終了してよい」と言われたときに呼ぶもの(窓を閉じてアプリを終える)
    pub fn set_on_quit(&self, f: impl Fn() + Send + Sync + 'static) {
        *self.on_quit.lock().unwrap() = Some(Box::new(f));
    }

    /// 受け付けていた Python が、言われもせずに居なくなったときに呼ぶもの
    /// (窓を読み直して、理由の画面を出す)
    pub fn set_on_lost(&self, f: impl Fn() + Send + Sync + 'static) {
        *self.on_lost.lock().unwrap() = Some(Box::new(f));
    }

    pub fn phase(&self) -> Phase {
        self.state.lock().unwrap().phase.clone()
    }

    pub fn failure(&self) -> Option<Failure> {
        self.state.lock().unwrap().failure.clone()
    }

    pub fn python(&self) -> String {
        self.state.lock().unwrap().python.clone()
    }

    pub fn stderr_tail(&self) -> Vec<String> {
        self.stderr.lock().unwrap().iter().cloned().collect()
    }

    /// 受け付けが始まるか、終わるまで待つ。始まっていれば `true`
    pub fn wait_started(&self, limit: Duration) -> bool {
        let deadline = Instant::now() + limit;
        let mut state = self.state.lock().unwrap();
        while state.phase == Phase::Starting {
            let now = Instant::now();
            if now >= deadline {
                return false;
            }
            state = self.changed.wait_timeout(state, deadline - now).unwrap().0;
        }
        state.phase == Phase::Started
    }

    fn set_phase(&self, phase: Phase, failure: Option<Failure>) {
        let mut state = self.state.lock().unwrap();
        // 一度失敗の理由が入ったら、あとの「終わった」で消さない
        if failure.is_some() || state.failure.is_none() {
            if failure.is_some() {
                state.failure = failure;
            }
        }
        state.phase = phase;
        self.changed.notify_all();
    }

    /// Python を探して起動する。**見つかるまで候補を順に試す。**
    ///
    /// Windows には「Python が入っていないのに `python.exe` がある」状態がある
    /// (Microsoft Store へ案内するだけの代役)。それは何も話さずにすぐ終わるので、
    /// 1行も返さずに終わった候補は「使えない」として次へ進む。
    pub fn start(self: &Arc<Self>) {
        let script = self.root.join("bridge.py");
        if !script.is_file() {
            self.set_phase(
                Phase::Ended,
                Some(Failure {
                    message: format!("bridge.py が見つかりません: {}", script.display()),
                    hint: "PackagingTool.exe は、アプリ一式が入ったフォルダの中に置いてください\
                           (ショートカットを作るのは大丈夫です)。"
                        .into(),
                    log_dir: String::new(),
                }),
            );
            return;
        }
        let mut tried = Vec::new();
        for (program, args) in python_candidates() {
            tried.push(program.clone());
            match self.spawn(&program, &args, &script) {
                Ok(()) => {}
                Err(_) => continue, // 見つからない
            }
            // 1行目(started / fatal)か、何も言わずに終わるのを待つ
            let mut state = self.state.lock().unwrap();
            while state.phase == Phase::Starting {
                state = self.changed.wait(state).unwrap();
            }
            let spoke = state.phase == Phase::Started || state.failure.is_some();
            drop(state);
            if spoke {
                return;
            }
            // 何も言わずに終わった。次の候補へ
            let mut state = self.state.lock().unwrap();
            state.phase = Phase::Starting;
        }
        self.set_phase(
            Phase::Ended,
            Some(Failure {
                message: "Python が見つかりません(または起動できません)".into(),
                hint: format!(
                    "https://www.python.org/downloads/ から Python 3.9 以上を入れてください。\
                     インストーラの最初の画面で「Add python.exe to PATH」に必ずチェックを入れます。\n\
                     入れたあと、コマンドプロンプトでこのフォルダへ移り、次を1度だけ実行してください:\n\
                     python -m pip install -r requirements.txt\n\n試したもの: {}",
                    tried.join(" / ")
                ),
                log_dir: String::new(),
            }),
        );
    }

    fn spawn(self: &Arc<Self>, program: &str, args: &[String], script: &Path) -> std::io::Result<()> {
        let mut command = Command::new(program);
        command
            .args(args)
            .arg("-X")
            .arg("utf8")
            .arg(script)
            .current_dir(&self.root)
            .env("PACKAGING_TOOL_TOKEN", &self.token)
            .env("PYTHONIOENCODING", "utf-8")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            // コンソールの窓を出さない
            const CREATE_NO_WINDOW: u32 = 0x0800_0000;
            command.creation_flags(CREATE_NO_WINDOW);
        }
        let mut child = command.spawn()?;
        let stdout = child.stdout.take().expect("stdout");
        let stderr = child.stderr.take().expect("stderr");
        *self.stdin.lock().unwrap() = child.stdin.take();
        *self.child.lock().unwrap() = Some(child);
        self.state.lock().unwrap().python = program.to_string();

        // 標準エラー: 末尾だけ覚える(Python のログもここに出る)
        let tail = self.stderr.clone();
        thread::Builder::new()
            .name("python-stderr".into())
            .spawn(move || {
                for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                    let mut tail = tail.lock().unwrap();
                    if tail.len() >= STDERR_LINES {
                        tail.pop_front();
                    }
                    tail.push_back(line);
                }
            })?;

        // 標準出力: 応答と知らせを読む
        let me = self.clone();
        thread::Builder::new()
            .name("python-stdout".into())
            .spawn(move || me.read_loop(stdout))?;
        Ok(())
    }

    fn read_loop(self: Arc<Self>, stdout: impl Read) {
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        let mut was_started = false;
        let mut said_quit = false;
        loop {
            line.clear();
            match reader.read_line(&mut line) {
                Ok(0) | Err(_) => break,
                Ok(_) => {}
            }
            let head: Value = match serde_json::from_str(line.trim_end()) {
                Ok(v) => v,
                Err(_) => continue, // 形の崩れた行は飛ばす(本来は来ない)
            };
            let len = head.get("len").and_then(Value::as_u64).unwrap_or(0) as usize;
            let mut body = vec![0u8; len];
            if len > 0 && reader.read_exact(&mut body).is_err() {
                break;
            }
            if let Some(event) = head.get("event").and_then(Value::as_str) {
                was_started |= event == "started";
                said_quit |= event == "quit";
                self.on_event(event, &head);
                continue;
            }
            let id = head.get("id").and_then(Value::as_u64).unwrap_or(0);
            let reply = Reply {
                status: head.get("status").and_then(Value::as_u64).unwrap_or(500) as u16,
                headers: head
                    .get("headers")
                    .and_then(Value::as_array)
                    .map(|items| {
                        items
                            .iter()
                            .filter_map(|pair| {
                                let pair = pair.as_array()?;
                                Some((pair.first()?.as_str()?.to_string(), pair.get(1)?.as_str()?.to_string()))
                            })
                            .collect()
                    })
                    .unwrap_or_default(),
                body,
            };
            if let Some(sender) = self.pending.lock().unwrap().remove(&id) {
                let _ = sender.send(reply);
            }
        }
        // 終わった。待っている要求には「答えられない」を返す(送り手を捨てる)
        self.pending.lock().unwrap().clear();
        self.set_phase(Phase::Ended, None);
        if was_started && !said_quit {
            if let Some(f) = self.on_lost.lock().unwrap().as_ref() {
                f();
            }
        }
    }

    fn on_event(&self, event: &str, head: &Value) {
        match event {
            "started" => self.set_phase(Phase::Started, None),
            "fatal" => {
                let text = |key: &str| head.get(key).and_then(Value::as_str).unwrap_or("").to_string();
                self.set_phase(
                    Phase::Ended,
                    Some(Failure { message: text("message"), hint: text("hint"), log_dir: text("log_dir") }),
                );
            }
            "quit" => {
                if let Some(f) = self.on_quit.lock().unwrap().as_ref() {
                    f();
                }
            }
            _ => {}
        }
    }

    /// 要求を1件渡して、応答を待つ。
    pub fn call(&self, method: &str, path: &str, query: &str, headers: Vec<(String, String)>, body: &[u8]) -> Result<Reply, String> {
        if self.phase() != Phase::Started {
            return Err("Python が動いていません".into());
        }
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let mut headers = headers;
        headers.retain(|(k, _)| !k.eq_ignore_ascii_case("x-tool-token"));
        headers.push(("X-Tool-Token".into(), self.token.clone()));
        let head = json!({
            "id": id,
            "method": method,
            "path": path,
            "query": query,
            "headers": headers.iter().map(|(k, v)| vec![k.clone(), v.clone()]).collect::<Vec<_>>(),
            "len": body.len(),
        });
        let (sender, receiver) = mpsc::channel();
        self.pending.lock().unwrap().insert(id, sender);
        {
            let mut stdin = self.stdin.lock().unwrap();
            let Some(pipe) = stdin.as_mut() else {
                self.pending.lock().unwrap().remove(&id);
                return Err("Python への通り道が閉じています".into());
            };
            let mut frame = serde_json::to_vec(&head).map_err(|e| e.to_string())?;
            frame.push(b'\n');
            frame.extend_from_slice(body);
            if let Err(e) = pipe.write_all(&frame).and_then(|_| pipe.flush()) {
                self.pending.lock().unwrap().remove(&id);
                return Err(format!("Python へ渡せませんでした: {e}"));
            }
        }
        receiver.recv_timeout(REPLY_TIMEOUT).map_err(|_| {
            self.pending.lock().unwrap().remove(&id);
            "Python から応答がありません".to_string()
        })
    }

    /// 終える。標準入力を閉じて Python に終わってもらい、終わらなければ止める。
    pub fn shutdown(&self, grace: Duration) {
        self.stdin.lock().unwrap().take(); // 閉じる = 「もう要求は来ない」
        let mut child = self.child.lock().unwrap();
        if let Some(child) = child.as_mut() {
            let deadline = Instant::now() + grace;
            while Instant::now() < deadline {
                if let Ok(Some(_)) = child.try_wait() {
                    return;
                }
                thread::sleep(Duration::from_millis(50));
            }
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

/// 試す Python の順番。`PACKAGING_TOOL_PYTHON` があればそれだけを使う。
fn python_candidates() -> Vec<(String, Vec<String>)> {
    if let Ok(path) = std::env::var("PACKAGING_TOOL_PYTHON") {
        if !path.trim().is_empty() {
            return vec![(path, vec![])];
        }
    }
    if cfg!(windows) {
        // python.exe → py ランチャ(PATH に入れ忘れても py は入っていることが多い)
        vec![("python.exe".into(), vec![]), ("py.exe".into(), vec!["-3".into()])]
    } else {
        vec![("python3".into(), vec![]), ("python".into(), vec![])]
    }
}

/// 起動ごとの合言葉。ほかのプロセスから Python へ要求は届かないが
/// (標準入出力は親子だけのもの)、アプリ側の確認はそのまま生かす。
pub fn new_token() -> String {
    use std::collections::hash_map::RandomState;
    use std::hash::{BuildHasher, Hasher};
    let mut text = String::new();
    for _ in 0..4 {
        let mut h = RandomState::new().build_hasher();
        h.write_u128(Instant::now().elapsed().as_nanos());
        h.write_u32(std::process::id());
        text.push_str(&format!("{:016x}", h.finish()));
    }
    text
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn 合言葉は毎回違い十分に長い() {
        let a = new_token();
        let b = new_token();
        assert_eq!(a.len(), 64);
        assert_ne!(a, b);
    }

    #[test]
    fn 環境変数があればそのpythonだけを使う() {
        std::env::set_var("PACKAGING_TOOL_PYTHON", "/opt/py/bin/python3");
        assert_eq!(python_candidates(), vec![("/opt/py/bin/python3".to_string(), vec![])]);
        std::env::remove_var("PACKAGING_TOOL_PYTHON");
    }
}
