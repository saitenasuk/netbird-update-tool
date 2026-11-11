// 登录页：调 /api/login 拿会话 cookie，成功后回主页面
const form = document.querySelector("#login-form");
const errorBox = document.querySelector("#login-error");
const submit = document.querySelector("#login-submit");

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorBox.hidden = true;
  submit.disabled = true;
  submit.textContent = "登录中…";

  try {
    const res = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user: document.querySelector("#login-user").value.trim(),
        password: document.querySelector("#login-pass").value,
      }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      showError(data.error || "登录失败");
      return;
    }
    location.href = "/";
  } catch (err) {
    showError("请求失败：" + err.message);
  } finally {
    submit.disabled = false;
    submit.textContent = "登录";
  }
});
