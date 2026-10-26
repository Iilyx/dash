var filters = [];
var slice = document.getElementById("slice");
var log = document.getElementById("log");
var form = document.getElementById("ask");
var question = document.getElementById("question");

function filterLabel(item) {
  var parts = [item.path, item.condition, item.value].filter(Boolean);
  if (item.value2) parts.push(item.value2);
  return parts.join(" ");
}

function renderSlice() {
  var labels = filters.map(filterLabel).filter(Boolean);
  slice.textContent = labels.length ? labels.join(" · ") : "Срез не выбран";
}

function addBubble(role, text) {
  var node = document.createElement("div");
  node.className = "bubble " + role;
  node.textContent = text;
  log.appendChild(node);
  log.scrollTop = log.scrollHeight;
  return node;
}

window.addEventListener("message", function (event) {
  if (event.origin !== window.location.origin) return;
  if (!event.data || event.data.type !== "dashboard-filters") return;
  filters = Array.isArray(event.data.filters) ? event.data.filters : [];
  renderSlice();
});

form.addEventListener("submit", async function (event) {
  event.preventDefault();
  var text = question.value.trim();
  if (!text) return;
  addBubble("user", text);
  question.value = "";
  var button = form.querySelector("button");
  button.disabled = true;
  var pending = addBubble("assistant", "Считаю…");
  try {
    var response = await fetch("/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: text, filters: filters })
    });
    var payload = await response.json();
    pending.textContent = payload.answer || payload.detail || "Пустой ответ";
  } catch (error) {
    pending.textContent = "Хост не ответил";
  } finally {
    button.disabled = false;
  }
});
