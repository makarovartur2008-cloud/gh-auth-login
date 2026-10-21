"use strict";

const tg = window.Telegram?.WebApp;
if (tg && tg.initData) {
  tg.ready();
  tg.expand();
  document.documentElement.classList.add("tg");
}

const state = { tab: "orders", day: null, today: null, products: [], me: null, period: "today" };
const PAY = { cash: "наличка", transfer: "перевод", debt: "в долг" };
const EXP = { fuel: "⛽ газ/бензин", purchase: "📦 закупка товара", other: "🧾 прочее" };
const WD = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"];

// ---------- утилиты ----------

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "value") el.value = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : String(kid));
  }
  return el;
}

function fill(el, ...kids) {
  el.replaceChildren(...kids.flat(Infinity).filter((k) => k !== null && k !== undefined && k !== false));
}

async function api(path, opts = {}) {
  const res = await fetch("/api" + path, {
    method: opts.method || "GET",
    headers: { "Content-Type": "application/json", "X-Init-Data": tg?.initData || "" },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let msg = "Ошибка " + res.status;
    try { msg = (await res.json()).detail || msg; } catch (_) {}
    if (typeof msg !== "string") msg = "Проверь заполнение полей";
    toast("⚠️ " + msg);
    throw new Error(msg);
  }
  return res.json();
}

const money = (v) => Math.round(v || 0).toLocaleString("ru-RU") + " ₽";
const qty = (q) => (Number.isInteger(q) ? q : +(+q).toFixed(2)).toString();
const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const parseDay = (s) => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
const addDays = (s, n) => { const d = parseDay(s); d.setDate(d.getDate() + n); return iso(d); };
function dayLabel(s) {
  const diff = Math.round((parseDay(s) - parseDay(state.today)) / 864e5);
  const base = `${parseDay(s).getDate()}.${String(parseDay(s).getMonth() + 1).padStart(2, "0")} ${WD[parseDay(s).getDay()]}`;
  return ({ 0: "Сегодня", 1: "Завтра", "-1": "Вчера" }[diff] || "") + (diff >= -1 && diff <= 1 ? ", " : "") + base;
}

function toast(text) {
  const t = document.getElementById("toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), 2500);
}

function haptic() { try { tg?.HapticFeedback?.impactOccurred("light"); } catch (_) {} }

function openSheet(...content) {
  const sheet = document.getElementById("sheet");
  const body = sheet.querySelector(".sheet-body");
  fill(body, h("button", { class: "sheet-close", onclick: closeSheet }, "✕"), ...content);
  sheet.hidden = false;
  body.scrollTop = 0;
}
function closeSheet() { document.getElementById("sheet").hidden = true; }
document.querySelector(".sheet-bg").addEventListener("click", closeSheet);

const view = () => document.getElementById("view");
function setTitle(t) { document.getElementById("title").textContent = t; }

function phoneLink(c) {
  if (!c.phone) return null;
  const digits = c.phone.replace(/\D/g, "");
  return h("a", { href: "tel:+" + (digits.length === 10 ? "7" + digits : digits.replace(/^8/, "7")) }, "📞 " + (c.phone_fmt || c.phone));
}

// ---------- вкладки ----------

document.querySelectorAll("#tabs button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
    state.tab = b.dataset.tab;
    render();
  })
);

function render() {
  document.querySelector(".fab")?.remove();
  ({ orders: renderOrders, clients: renderClients, money: renderMoney, prices: renderPrices })[state.tab]();
}

// ---------- заказы ----------

function orderCard(o, { overdue = false } = {}) {
  const status = { new: ["🟡 ждёт", "yellow"], done: ["✅ " + (PAY[o.payment] || ""), o.payment === "debt" ? "red" : "green"],
    cancelled: ["❌ отменён", ""] }[o.status];
  const c = o.client;
  const act = async (body, msg) => {
    haptic();
    await api(`/orders/${o.id}`, { method: "PATCH", body });
    toast(msg);
    renderOrders();
  };
  const actions = o.status === "new"
    ? h("div", { class: "actions" },
        h("button", { class: "green small", onclick: () => act({ status: "done", payment: "cash" }, "Доставлено, наличка") }, "✅ Наличка"),
        h("button", { class: "green small", onclick: () => act({ status: "done", payment: "transfer" }, "Доставлено, перевод") }, "✅ Перевод"),
        h("button", { class: "small", onclick: () => act({ status: "done", payment: "debt" }, "Записал в долг") }, "📝 Долг"),
        h("button", { class: "small", onclick: () => act({ date: addDays(o.date < state.today ? state.today : o.date, 1) }, "Перенесено") }, "➡️"),
      )
    : o.status === "done" && o.payment === "debt"
      ? h("div", { class: "actions" },
          h("button", { class: "small", onclick: () => act({ status: "done", payment: "cash" }, "Оплачено") }, "💵 Оплатил наличкой"),
          h("button", { class: "small", onclick: () => act({ status: "done", payment: "transfer" }, "Оплачено") }, "💳 Перевод"))
      : null;
  return h("div", { class: `card ${o.status}${overdue ? " overdue" : ""}` },
    h("div", { class: "row between" },
      h("div", { class: "grow", onclick: () => orderForm(o) },
        h("div", { class: "name" }, c.name, " ", o.recurring_id ? "🔁" : "", o.via_friend ? " 🤝" : "", o.source ? " 🌐" : ""),
        o.source ? h("div", { class: "hint" }, "🌐 ", o.source) : null,
        overdue ? h("div", { class: "hint" }, "⚠️ не доставлен — ", dayLabel(o.date)) : null,
        c.label ? h("div", { class: "hint" }, "📍 ", c.label) : null),
      h("span", { class: "badge " + status[1] }, status[0])),
    h("div", { class: "items", onclick: () => orderForm(o) },
      o.items.map((i) => h("div", {}, `${i.emoji || "•"} ${i.name} — ${qty(i.qty)} ${i.unit} × ${qty(i.price)} = ${money(i.sum)}`)),
      o.notes && o.notes !== "🔁 постоянный" ? h("div", { class: "hint" }, "📝 ", o.notes) : null,
      c.notes ? h("div", { class: "hint" }, "ℹ️ ", c.notes) : null),
    h("div", { class: "row between" }, phoneLink(c) || h("span"), h("span", { class: "sum" }, money(o.total))),
    actions);
}

function plannedCard(r) {
  return h("div", { class: "card planned" },
    h("div", { class: "row between" }, h("div", { class: "name" }, "🔁 ", r.client_name), h("span", { class: "badge" }, "по графику")),
    h("div", { class: "items" }, r.items.map((i) => h("div", {}, `${i.emoji || "•"} ${i.name} — ${qty(i.qty)} ${i.unit}`))),
    h("div", { class: "row between" }, h("span", { class: "hint" }, `каждые ${r.every_days} дн.`), h("span", { class: "sum" }, money(r.total))));
}

async function renderOrders() {
  setTitle("Заказы");
  const day = state.day;
  const start = addDays(state.today, -1);
  const [data, counts] = await Promise.all([api(`/orders?day=${day}`), api(`/orders/counts?start=${start}&days=21`)]);
  const strip = h("div", { class: "dates" });
  for (let i = 0; i < 21; i++) {
    const d = addDays(start, i);
    const dd = parseDay(d);
    strip.append(h("button", { class: d === day ? "active" : "", onclick: () => { state.day = d; renderOrders(); } },
      h("small", {}, i === 1 ? "сегодня" : i === 2 ? "завтра" : WD[dd.getDay()]), h("b", {}, dd.getDate()),
      counts[d] ? h("i", {}, counts[d]) : null));
  }
  const pick = h("input", { type: "date", value: day, onchange: (e) => { if (e.target.value) { state.day = e.target.value; renderOrders(); } } });

  const active = data.orders.filter((o) => o.status === "new");
  const done = data.orders.filter((o) => o.status === "done");
  const kg = active.flatMap((o) => o.items).filter((i) => i.unit === "кг").reduce((a, i) => a + i.qty, 0);
  const load = {};
  active.forEach((o) => o.items.forEach((i) => (load[i.name] = (load[i.name] || 0) + i.qty)));

  const list = [];
  data.overdue.forEach((o) => list.push(orderCard(o, { overdue: true })));
  active.forEach((o) => list.push(orderCard(o)));
  data.planned.forEach((r) => list.push(plannedCard(r)));
  done.forEach((o) => list.push(orderCard(o)));
  data.orders.filter((o) => o.status === "cancelled").forEach((o) => list.push(orderCard(o)));

  const strip_wrap = h("div", {}, strip);
  fill(view(), 
    strip_wrap,
    h("div", { class: "row" }, h("div", { class: "grow" }, h("b", {}, dayLabel(day))), h("div", { style: "width:150px" }, pick)),
    h("div", { class: "summary", style: "margin-top:8px" },
      h("div", { class: "card" }, h("div", { class: "hint" }, "Отвезти"), h("div", { class: "big" }, active.length + data.planned.length)),
      h("div", { class: "card" }, h("div", { class: "hint" }, "Получить"), h("div", { class: "big" }, money(active.reduce((a, o) => a + o.total, 0)))),
      h("div", { class: "card" }, h("div", { class: "hint" }, "Груз"), h("div", { class: "big" }, qty(Math.round(kg)) + " кг"))),
    Object.keys(load).length
      ? h("div", { class: "card" }, h("div", { class: "hint" }, "Загрузить в машину"),
          Object.entries(load).map(([n, q]) => h("div", {}, `• ${n}: ${qty(q)}`)))
      : null,
    list.length ? list : h("div", { class: "empty" }, "На этот день заказов нет"),
  );
  strip.querySelector(".active")?.scrollIntoView({ inline: "center", block: "nearest" });
  document.body.append(h("button", { class: "primary fab", onclick: () => orderForm(null, { date: day }) }, "＋ Заказ"));
}

// ---------- форма заказа ----------

async function orderForm(order, preset = {}) {
  const isNew = !order;
  let client = order?.client || preset.client || null;
  const products = state.products.filter((p) => p.active);
  const items = order ? order.items.map((i) => ({ ...i })) : [{ product_id: products[0]?.id, qty: products[0]?.min_qty || 1, price: products[0]?.price }];

  const dateIn = h("input", { type: "date", value: order?.date || preset.date || state.today });
  const notesIn = h("textarea", { placeholder: "Заметка к заказу" }, order?.notes || "");
  const friendIn = h("input", { type: "checkbox" });
  friendIn.checked = order ? order.via_friend : state.me.role !== "owner";
  const everyIn = h("select", {}, h("option", { value: "" }, "нет"), h("option", { value: "7" }, "каждую неделю"),
    h("option", { value: "14" }, "раз в 2 недели"), h("option", { value: "30" }, "раз в месяц"));

  // клиент
  const clientBox = h("div");
  const newFields = {
    name: h("input", { placeholder: "Имя" }), phone: h("input", { placeholder: "Телефон", type: "tel" }),
    address: h("input", { placeholder: "Адрес (улица, дом)" }), entrance: h("input", { placeholder: "Подъезд" }),
    floor: h("input", { placeholder: "Этаж" }), apartment: h("input", { placeholder: "Кв." }),
  };
  function drawClient() {
    if (client) {
      fill(clientBox, h("div", { class: "card row between" },
        h("div", {}, h("div", { class: "name" }, client.name), h("div", { class: "hint" }, [client.phone_fmt, client.label].filter(Boolean).join(" · "))),
        isNew ? h("button", { class: "small", onclick: () => { client = null; drawClient(); } }, "Сменить") : null));
      return;
    }
    const search = h("input", { placeholder: "🔍 Найти клиента: имя, телефон, адрес" });
    const results = h("div", { class: "pick" });
    let timer;
    search.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const q = search.value.trim();
        if (!q) return fill(results, );
        const found = await api(`/clients?q=${encodeURIComponent(q)}`);
        fill(results, ...found.slice(0, 20).map((c) =>
          h("div", { onclick: () => { client = c; drawClient(); } }, h("b", {}, c.name), " ", h("span", { class: "hint" }, c.phone_fmt, " ", c.label))));
      }, 250);
    });
    fill(clientBox, search, results,
      h("label", {}, "…или новый клиент:"), newFields.name, h("div", { style: "height:6px" }), newFields.phone,
      h("div", { style: "height:6px" }), newFields.address,
      h("div", { class: "grid3", style: "margin-top:6px" }, newFields.entrance, newFields.floor, newFields.apartment));
  }
  drawClient();

  // товары
  const itemsBox = h("div");
  function drawItems() {
    fill(itemsBox, h("div", { class: "item-row hint" }, h("span", {}, "Товар"), h("span", {}, "Кол-во"), h("span", {}, "Цена"), h("span")),
      ...items.map((it, idx) => {
        const sel = h("select", { onchange: (e) => {
          const p = products.find((x) => x.id === +e.target.value);
          it.product_id = p.id; it.price = p.price; if (p.min_qty && it.qty < p.min_qty) it.qty = p.min_qty; drawItems();
        } }, products.map((p) => h("option", { value: p.id }, `${p.emoji} ${p.name}`)));
        sel.value = it.product_id;
        return h("div", { class: "item-row" }, sel,
          h("input", { type: "number", inputmode: "decimal", value: it.qty, oninput: (e) => { it.qty = +e.target.value; drawTotal(); } }),
          h("input", { type: "number", inputmode: "decimal", value: it.price, oninput: (e) => { it.price = +e.target.value; drawTotal(); } }),
          h("button", { class: "red", onclick: () => { items.splice(idx, 1); drawItems(); } }, "✕"));
      }),
      h("div", { class: "row", style: "gap:6px;flex-wrap:wrap" },
        h("button", { class: "small", onclick: () => { const p = products[0]; items.push({ product_id: p.id, qty: p.min_qty || 1, price: p.price }); drawItems(); } }, "＋ товар"),
        h("button", { class: "small", onclick: () => { const p = products.find((x) => x.min_qty) || products[0]; items.push({ product_id: p.id, qty: state.me.sack_kg * 2, price: p.price }); drawItems(); } },
          `＋ 2 сетки картошки (${state.me.sack_kg * 2} кг)`)),
      totalEl);
    drawTotal();
  }
  const totalEl = h("div", { class: "line total" });
  function drawTotal() {
    const t = items.reduce((a, i) => a + (i.qty || 0) * (i.price || 0), 0);
    const warn = items.some((i) => { const p = products.find((x) => x.id === i.product_id); return p?.min_qty && i.qty < p.min_qty; });
    fill(totalEl, h("span", {}, "Итого", warn ? h("span", { class: "hint" }, " (⚠️ картошка от 50 кг)") : ""), h("span", {}, money(t)));
  }
  drawItems();

  async function save() {
    const body = { date: dateIn.value, notes: notesIn.value, via_friend: friendIn.checked,
      items: items.filter((i) => i.qty > 0).map((i) => ({ product_id: i.product_id, qty: i.qty, price: i.price })) };
    if (isNew) {
      if (client) body.client_id = client.id;
      else body.client = Object.fromEntries(Object.entries(newFields).map(([k, el]) => [k, el.value]));
      if (everyIn.value) body.every_days = +everyIn.value;
      await api("/orders", { method: "POST", body });
    } else {
      await api(`/orders/${order.id}`, { method: "PATCH", body });
    }
    haptic(); toast("Сохранено"); closeSheet(); render();
  }

  const extra = isNew
    ? [h("label", {}, "Постоянный клиент (повторять заказ)"), everyIn]
    : [h("div", { class: "actions" },
        order.status !== "new" ? h("button", { onclick: async () => { await api(`/orders/${order.id}`, { method: "PATCH", body: { status: "new" } }); closeSheet(); render(); } }, "↩️ Вернуть в доставку") : null,
        order.status === "new" ? h("button", { class: "red", onclick: async () => { await api(`/orders/${order.id}`, { method: "PATCH", body: { status: "cancelled" } }); toast("Отменён"); closeSheet(); render(); } }, "❌ Отменить") : null,
        !order.recurring_id ? h("button", { onclick: () => makeRegular(order) }, "🔁 Сделать постоянным") : null,
        h("button", { onclick: () => clientCard(order.client.id) }, "👤 Карточка клиента"),
        h("button", { class: "red", onclick: async () => { if (confirm("Удалить заказ совсем?")) { await api(`/orders/${order.id}`, { method: "DELETE" }); closeSheet(); render(); } } }, "🗑"))];

  openSheet(
    h("h2", { style: "margin-top:0" }, isNew ? "Новый заказ" : `Заказ №${order.id}`),
    h("label", {}, "Клиент"), clientBox,
    h("label", {}, "Дата доставки"), dateIn,
    h("label", {}, "Товары"), itemsBox,
    notesIn,
    h("label", { class: "check" }, friendIn, ` Заявка через друга (−${state.me.friend_fee} ₽)`),
    ...extra,
    h("div", { style: "height:10px" }),
    h("button", { class: "primary wide", onclick: save }, "💾 Сохранить"),
  );
}

async function makeRegular(order) {
  const days = prompt("Каждые сколько дней повторять заказ?", "7");
  if (!days || !(+days > 0)) return;
  await api(`/recurring/from-order/${order.id}?every_days=${+days}`, { method: "POST" });
  toast("Теперь клиент постоянный 🔁");
  closeSheet(); render();
}

// ---------- клиенты ----------

async function renderClients() {
  setTitle("Клиенты");
  const search = h("input", { placeholder: "🔍 Имя, телефон или адрес", value: state.clientQ || "" });
  const list = h("div");
  async function load() {
    const clients = await api(`/clients?q=${encodeURIComponent(search.value.trim())}`);
    state.clientQ = search.value;
    const regular = clients.filter((c) => c.regular);
    const rest = clients.filter((c) => !c.regular);
    const row = (c) => h("div", { class: "list-item", onclick: () => clientCard(c.id) },
      h("div", { class: "grow" }, h("div", { class: "name" }, c.regular ? "🔁 " : "", c.name), h("div", { class: "hint" }, [c.phone_fmt, c.label].filter(Boolean).join(" · "))),
      h("span", { class: "hint" }, "›"));
    fill(list, 
      clients.length ? null : h("div", { class: "empty" }, "Клиентов пока нет. Добавь голосом в боте или кнопкой ниже."),
      regular.length ? h("h2", {}, `Постоянные (${regular.length})`) : null,
      regular.length ? h("div", { class: "card" }, regular.map(row)) : null,
      rest.length ? h("h2", {}, `Все (${rest.length})`) : null,
      rest.length ? h("div", { class: "card" }, rest.map(row)) : null);
  }
  let timer;
  search.addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(load, 250); });
  fill(view(), search, list);
  await load();
  document.body.append(h("button", { class: "primary fab", onclick: () => clientForm() }, "＋ Клиент"));
}

function clientForm(c = null) {
  const f = {};
  const field = (k, ph, type = "text") => (f[k] = h("input", { placeholder: ph, type, value: c?.[k] || "" }));
  openSheet(
    h("h2", { style: "margin-top:0" }, c ? "Изменить клиента" : "Новый клиент"),
    h("label", {}, "Имя"), field("name", "Имя"),
    h("label", {}, "Телефон"), field("phone", "+7 …", "tel"),
    h("label", {}, "Адрес"), field("address", "Улица, дом"),
    h("div", { class: "grid3" },
      h("div", {}, h("label", {}, "Подъезд"), field("entrance", "")),
      h("div", {}, h("label", {}, "Этаж"), field("floor", "")),
      h("div", {}, h("label", {}, "Кв."), field("apartment", ""))),
    h("label", {}, "Заметки (домофон, когда звонить и т.п.)"),
    (f.notes = h("textarea", {}, c?.notes || "")),
    h("div", { style: "height:10px" }),
    h("button", { class: "primary wide", onclick: async () => {
      const body = Object.fromEntries(Object.entries(f).map(([k, el]) => [k, el.value]));
      const saved = c ? await api(`/clients/${c.id}`, { method: "PUT", body }) : await api("/clients", { method: "POST", body });
      toast("Сохранено"); clientCard(saved.id); if (state.tab === "clients") renderClients();
    } }, "💾 Сохранить"));
}

async function clientCard(id) {
  const c = await api(`/clients/${id}`);
  const regs = c.recurring.map((r) => {
    const every = h("input", { type: "number", value: r.every_days, style: "width:70px" });
    const next = h("input", { type: "date", value: r.next_date });
    return h("div", { class: "card" },
      h("div", { class: "row between" }, h("b", {}, "🔁 Постоянный заказ"), h("span", { class: "badge " + (r.active ? "green" : "") }, r.active ? "активен" : "на паузе")),
      h("div", { class: "items" }, r.items.map((i) => h("div", {}, `${i.emoji} ${i.name} — ${qty(i.qty)} ${i.unit} × ${qty(i.price)}`)), h("b", {}, money(r.total))),
      h("div", { class: "row" }, "каждые", every, "дн., след.:", next),
      h("div", { class: "actions" },
        h("button", { class: "small", onclick: async () => { await api(`/recurring/${r.id}`, { method: "PUT", body: { every_days: +every.value, next_date: next.value } }); toast("Сохранено"); clientCard(id); } }, "💾 Сохранить"),
        h("button", { class: "small", onclick: async () => { await api(`/recurring/${r.id}`, { method: "PUT", body: { active: !r.active } }); clientCard(id); } }, r.active ? "⏸ Пауза" : "▶️ Включить")));
  });
  openSheet(
    h("div", { class: "row between" }, h("h2", { style: "margin:0" }, c.name), h("button", { class: "small", onclick: () => clientForm(c) }, "✏️ Изменить")),
    h("div", { class: "card" },
      c.phone ? h("div", {}, phoneLink(c)) : h("div", { class: "hint" }, "телефон не указан"),
      c.label ? h("div", {}, "📍 ", c.label) : null,
      c.notes ? h("div", { class: "hint" }, "ℹ️ ", c.notes) : null),
    h("div", { class: "summary" },
      h("div", { class: "card" }, h("div", { class: "hint" }, "Заказов"), h("div", { class: "big" }, c.stats.orders)),
      h("div", { class: "card" }, h("div", { class: "hint" }, "Всего купил"), h("div", { class: "big" }, money(c.stats.revenue))),
      c.stats.debt ? h("div", { class: "card" }, h("div", { class: "hint" }, "Долг"), h("div", { class: "big", style: "color:var(--red)" }, money(c.stats.debt))) : null),
    regs,
    h("button", { class: "primary wide", style: "margin:8px 0", onclick: () => orderForm(null, { client: c }) }, "＋ Новый заказ"),
    c.orders.length ? h("h2", {}, "История") : null,
    c.orders.map((o) => h("div", { class: "card", onclick: () => orderForm(o) },
      h("div", { class: "row between" }, h("b", {}, dayLabel(o.date)), h("span", {}, money(o.total), " ", { new: "🟡", done: o.payment === "debt" ? "📝" : "✅", cancelled: "❌" }[o.status])),
      h("div", { class: "hint" }, o.items.map((i) => `${i.name} ${qty(i.qty)} ${i.unit}`).join(", ")))));
}

// ---------- финансы ----------

function periodRange(p) {
  const t = parseDay(state.today);
  if (p === "week") { const d = new Date(t); d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); return [iso(d), state.today]; }
  if (p === "month") return [iso(new Date(t.getFullYear(), t.getMonth(), 1)), state.today];
  if (p === "prev") { return [iso(new Date(t.getFullYear(), t.getMonth() - 1, 1)), iso(new Date(t.getFullYear(), t.getMonth(), 0))]; }
  return [state.today, state.today];
}

async function renderMoney() {
  setTitle("Финансы");
  const [start, end] = periodRange(state.period);
  const [r, exps, debts, sources] = await Promise.all([api(`/report?start=${start}&end=${end}`), api(`/expenses?start=${start}&end=${end}`),
    api("/orders/debts"), api(`/sources?start=${start}&end=${end}`)]);
  const seg = h("div", { class: "seg" }, [["today", "Сегодня"], ["week", "Неделя"], ["month", "Месяц"], ["prev", "Прошлый мес."]].map(([k, t]) =>
    h("button", { class: state.period === k ? "active" : "", onclick: () => { state.period = k; renderMoney(); } }, t)));
  const line = (a, b, style) => h("div", { class: "line", style }, h("span", {}, a), h("span", {}, b));
  fill(view(), 
    seg,
    h("div", { class: "card" },
      h("div", { class: "hint" }, "Чистая прибыль"),
      h("div", { class: "big", style: `font-size:30px;color:${r.profit >= 0 ? "var(--green)" : "var(--red)"}` }, money(r.profit)),
      h("div", { class: "hint" }, `доставлено заказов: ${r.orders}` + (r.open_orders ? `, ещё в доставке: ${r.open_orders}` : ""))),
    h("div", { class: "card" },
      line("Выручка", money(r.revenue), "font-weight:600"),
      line("   наличка", money(r.cash)), line("   перевод", money(r.transfer)),
      r.debt ? line("   в долг", money(r.debt)) : null,
      line("Закупочная стоимость товара", "−" + money(r.cogs)),
      r.friend_fees ? line("Другу за заявки", "−" + money(r.friend_fees)) : null,
      Object.entries(r.expenses).map(([k, v]) => line(EXP[k] || k, "−" + money(v))),
      h("div", { class: "line total" }, h("span", {}, "Прибыль"), h("span", {}, money(r.profit))),
      r.purchases ? h("div", { class: "hint", style: "margin-top:6px" }, `Закуплено товара за период: ${money(r.purchases)} (в прибыль не входит — учтено через закупочную стоимость)`) : null),
    Object.keys(r.by_product).length ? h("div", { class: "card" }, h("b", {}, "Продано"),
      Object.entries(r.by_product).map(([n, p]) => line(`${n}: ${qty(p.qty)} ${p.unit}`, money(p.revenue)))) : null,
    sources.length ? h("div", { class: "card" }, h("b", {}, "🌐 Заказы с сайта / QR"),
      sources.map((x) => line(`${x.source}: ${x.orders} заяв.` + (x.done ? `, доставлено ${x.done}` : ""), money(x.revenue)))) : null,
    debts.length ? h("h2", {}, `📝 Долги: ${money(debts.reduce((a, o) => a + o.total, 0))}`) : null,
    debts.map((o) => orderCard(o)),
    h("h2", {}, "Расходы"),
    h("button", { class: "wide", onclick: expenseForm }, "＋ Добавить расход"),
    exps.length ? h("div", { class: "card", style: "margin-top:8px" }, exps.map((e) => h("div", { class: "list-item" },
      h("div", { class: "grow" }, h("div", {}, EXP[e.category] || e.category), h("div", { class: "hint" }, dayLabel(e.date), e.note ? " · " + e.note : "")),
      h("b", {}, money(e.amount)),
      h("button", { class: "small red", onclick: async () => { if (confirm("Удалить расход?")) { await api(`/expenses/${e.id}`, { method: "DELETE" }); renderMoney(); } } }, "✕"))))
      : h("div", { class: "empty" }, "Расходов за период нет"),
    h("p", { class: "hint" }, "Прибыль = выручка − закупочная цена проданного − 50 ₽ другу за каждую его заявку − расходы (газ, прочее)."));
}

function expenseForm() {
  const cat = h("select", {}, Object.entries(EXP).map(([k, v]) => h("option", { value: k }, v)));
  const amount = h("input", { type: "number", inputmode: "numeric", placeholder: "Сумма, ₽" });
  const date = h("input", { type: "date", value: state.today });
  const note = h("input", { placeholder: "Комментарий" });
  openSheet(h("h2", { style: "margin-top:0" }, "Новый расход"),
    h("label", {}, "Что"), cat, h("label", {}, "Сумма"), amount, h("label", {}, "Дата"), date, h("label", {}, "Комментарий"), note,
    h("div", { style: "height:10px" }),
    h("button", { class: "primary wide", onclick: async () => {
      await api("/expenses", { method: "POST", body: { category: cat.value, amount: +amount.value, date: date.value, note: note.value } });
      toast("Записано"); closeSheet(); renderMoney();
    } }, "💾 Сохранить"));
  amount.focus();
}

// ---------- цены ----------

async function renderPrices() {
  setTitle("Цены и товары");
  state.products = await api("/products");
  const qrLabel = h("input", { placeholder: "Метка: например «Ленина 5»" });
  const openFlyer = () => {
    const url = `${location.origin}/flyer?s=${encodeURIComponent(qrLabel.value.trim())}`;
    if (tg?.openLink) tg.openLink(url); else window.open(url, "_blank");
  };
  const orderUrl = `${location.origin}/order`;
  fill(view(), 
    h("div", { class: "card" },
      h("b", {}, "🖨 Листовки с QR-кодом"),
      h("p", { class: "hint", style: "margin:4px 0 8px" },
        "Клиент сканирует код — открывается страница заказа, заявка приходит в бот. " +
        "Для каждого дома делай свою метку — в «Финансах» будет видно, откуда заказы."),
      qrLabel,
      h("div", { class: "actions" },
        h("button", { class: "primary", onclick: openFlyer }, "Открыть листовку для печати"),
        h("button", { onclick: async () => { try { await navigator.clipboard.writeText(orderUrl); toast("Ссылка скопирована"); } catch (_) { toast(orderUrl); } } }, "📋 Ссылка для Авито"))),
    h("p", { class: "hint" }, "Цена — за сколько продаёшь, закупка — за сколько берёшь (для расчёта прибыли). Нажми на товар, чтобы изменить."),
    h("div", { class: "card" }, state.products.map((p) => h("div", { class: "list-item", onclick: () => productForm(p), style: p.active ? "" : "opacity:.5" },
      h("div", { class: "grow" }, h("div", { class: "name" }, `${p.emoji} ${p.name}`),
        h("div", { class: "hint" }, `закупка ${qty(p.cost)} ₽` + (p.min_qty ? ` · от ${qty(p.min_qty)} ${p.unit}` : "") + (p.active ? "" : " · скрыт"))),
      h("b", { style: p.price ? "" : "color:var(--red)" }, p.price ? `${qty(p.price)} ₽/${p.unit}` : "цена?")))));
  document.body.append(h("button", { class: "primary fab", onclick: () => productForm() }, "＋ Товар"));
}

function productForm(p = null) {
  const f = {
    emoji: h("input", { value: p?.emoji || "", placeholder: "🥔" }),
    name: h("input", { value: p?.name || "", placeholder: "Название" }),
    unit: h("select", {}, h("option", { value: "кг" }, "кг"), h("option", { value: "шт" }, "шт")),
    price: h("input", { type: "number", inputmode: "decimal", value: p?.price ?? "" }),
    cost: h("input", { type: "number", inputmode: "decimal", value: p?.cost ?? "" }),
    min_qty: h("input", { type: "number", inputmode: "decimal", value: p?.min_qty ?? 0 }),
  };
  f.unit.value = p?.unit || "кг";
  const active = h("input", { type: "checkbox" });
  active.checked = p ? p.active : true;
  openSheet(h("h2", { style: "margin-top:0" }, p ? "Изменить товар" : "Новый товар"),
    h("div", { style: "display:grid;grid-template-columns:60px 1fr;gap:8px" }, f.emoji, f.name),
    h("div", { class: "grid2" }, h("div", {}, h("label", {}, "Цена продажи, ₽"), f.price), h("div", {}, h("label", {}, "Закупка, ₽"), f.cost)),
    h("div", { class: "grid2" }, h("div", {}, h("label", {}, "Единица"), f.unit), h("div", {}, h("label", {}, "Минимум в заказе"), f.min_qty)),
    h("label", { class: "check" }, active, " Есть в продаже"),
    h("button", { class: "primary wide", onclick: async () => {
      const body = { name: f.name.value, unit: f.unit.value, emoji: f.emoji.value, price: +f.price.value || 0,
        cost: +f.cost.value || 0, min_qty: +f.min_qty.value || 0, active: active.checked };
      await api(p ? `/products/${p.id}` : "/products", { method: p ? "PUT" : "POST", body });
      toast("Сохранено"); closeSheet(); renderPrices();
    } }, "💾 Сохранить"));
}

// ---------- старт ----------

(async function init() {
  try {
    state.me = await api("/me");
  } catch (e) {
    fill(view(), h("div", { class: "empty" }, "⛔ ", e.message));
    return;
  }
  state.today = state.me.today;
  state.day = state.today;
  state.products = await api("/products");
  render();
})();
