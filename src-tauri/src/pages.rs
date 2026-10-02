//! 外枠が自分で出す画面。**Python がまだ居ない / 居なくなった**ときだけ使う。
//! ふだんの画面(待機画面も含む)は Python が返す。

use crate::bridge::Failure;

fn escape(text: &str) -> String {
    text.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
}

const STYLE: &str = r#"
 body{margin:0;min-height:100vh;display:grid;place-items:center;background:#eef1f5;color:#101720;
      font-family:system-ui,"Yu Gothic UI","Meiryo UI",sans-serif;line-height:1.7}
 .box{width:min(640px,calc(100vw - 48px));background:#fff;border:1px solid #c9d2dc;border-radius:4px;
      padding:28px 32px;box-shadow:0 6px 20px rgba(16,23,32,.08)}
 h1{margin:0 0 12px;font-size:19px}
 h1.ng{color:#b4232a}
 .hint{margin-top:16px;padding:14px;background:#fdeaea;border-left:4px solid #b4232a;white-space:pre-wrap}
 dt{color:#556171;font-size:13px;margin-top:14px}
 code,pre{font-family:ui-monospace,Consolas,monospace;font-size:12px;background:rgba(0,0,0,.05);
          padding:2px 5px;border-radius:2px;word-break:break-all}
 pre{padding:10px;max-height:16em;overflow:auto;white-space:pre-wrap}
 button{font:inherit;padding:8px 18px;margin-top:16px;cursor:pointer}
"#;

/// Python が「受け付け始めた」と言う前の、ほんの一瞬に出す画面。
/// 1秒ごとに読み直し、Python の待機画面(段と進み具合が出る)へ移る。
pub fn starting() -> String {
    format!(
        r#"<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="1"><title>梱包資材総合ツール</title><style>{STYLE}</style></head>
<body><main class="box"><h1>起動しています…</h1><p>しばらくお待ちください。</p></main></body></html>"#
    )
}

/// 移り先へ移るだけのページ(画面の 302 の代わり)。
/// 移り先はアプリの中の経路だけにする(外へは移らない)。
pub fn moving_to(target: &str) -> String {
    let inside = target.starts_with('/') && !target.starts_with("//");
    let target = if inside { target } else { "/" };
    // `</script>` で抜け出させない(JSON は `<` をそのまま書くので、自分で崩す)
    let quoted = serde_json::to_string(target)
        .unwrap_or_else(|_| "\"/\"".into())
        .replace('<', "\\u003c");
    format!(
        r#"<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="0;url={url}"><title>梱包資材総合ツール</title></head>
<body><script>location.replace({quoted});</script></body></html>"#,
        url = escape(target)
    )
}

/// 起動できなかった / 途中で止まった。**次に何をすればよいか**まで出す。
pub fn failure(failure: Option<&Failure>, python: &str, stderr: &[String], log_hint: &str) -> String {
    let (message, hint, log_dir) = match failure {
        Some(f) => (f.message.clone(), f.hint.clone(), f.log_dir.clone()),
        None => (
            "Python の処理が止まりました".to_string(),
            "もう一度起動してください。続けて起きるときは、下の「最後の出力」と\
             ログを担当に送ってください。"
                .to_string(),
            String::new(),
        ),
    };
    let log_dir = if log_dir.is_empty() { log_hint.to_string() } else { log_dir };
    let tail = stderr.iter().rev().take(25).rev().cloned().collect::<Vec<_>>().join("\n");
    format!(
        r#"<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>起動できませんでした</title>
<style>{STYLE}</style></head><body><main class="box">
<h1 class="ng">起動できませんでした</h1>
<p>{message}</p>
{hint}
<dt>ログの場所</dt><p><code>{log_dir}</code></p>
<dt>使った Python</dt><p><code>{python}</code></p>
{tail}
<button type="button" onclick="location.reload()">もう一度試す</button>
</main></body></html>"#,
        message = escape(&message),
        hint = if hint.is_empty() { String::new() } else { format!(r#"<div class="hint">{}</div>"#, escape(&hint)) },
        log_dir = escape(if log_dir.is_empty() { "(分かりません)" } else { &log_dir }),
        python = escape(if python.is_empty() { "(見つかっていません)" } else { python }),
        tail = if tail.is_empty() {
            String::new()
        } else {
            format!("<dt>最後の出力</dt><pre>{}</pre>", escape(&tail))
        },
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn 理由と次の一手と出力が出て_タグは無害化される() {
        let f = Failure { message: "<b>だめ</b>".into(), hint: "pip で入れる".into(), log_dir: "C:\\logs".into() };
        let html = failure(Some(&f), "python.exe", &["Traceback <x>".into()], "");
        assert!(html.contains("&lt;b&gt;だめ&lt;/b&gt;"));
        assert!(html.contains("pip で入れる"));
        assert!(html.contains("C:\\logs"));
        assert!(html.contains("Traceback &lt;x&gt;"));
        assert!(html.contains("python.exe"));
    }

    #[test]
    fn 移り先はアプリの中だけ() {
        assert!(moving_to("/lot?x=1").contains(r#"location.replace("/lot?x=1")"#));
        assert!(moving_to("https://evil.example/").contains(r#"location.replace("/")"#));
        assert!(moving_to("//evil.example/").contains(r#"location.replace("/")"#));
        let tricky = moving_to("/a\"</script><script>alert(1)</script>");
        assert_eq!(tricky.matches("</script>").count(), 1, "{tricky}");
    }

    #[test]
    fn 理由が無ければ止まったと言う() {
        let html = failure(None, "", &[], "D:\\ログ");
        assert!(html.contains("止まりました"));
        assert!(html.contains("D:\\ログ"));
        assert!(html.contains("見つかっていません"));
    }
}
