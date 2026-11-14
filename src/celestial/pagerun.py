"""作ったページの JavaScript を、node の上で実際に動かして確かめる(ブラウザーの代わり)。

構文の確かめ(node --check)では、動かしたときの誤りは分からない。小さいAIは、HTML に無い要素を JavaScript で探す
(querySelector('.weather') が null になり、そこで止まる)や、天気を番号のまま書き込むのを見落としがち
(qwen2.5-coder:7b で実際にあった。ページを開く確かめは HTTP 200 で通っていた)。

ページの HTML から要素(id・class・タグ)を作り、fetch には、確かめで開いた同じサーバーの答えを返す。そのほかの
ブラウザーの機能は、何をしてもエラーにならない代わりのもので受ける(誤った知らせを出さない側に倒す)。外のサイトは
開かない(その fetch は、答えが返らないままにする)。タイマー(時計など)は、決まった回数だけ呼ぶ。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .codecheck import _node

_RUNNER = r"""'use strict';
const fs = require('fs');
const vm = require('vm');
const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const started = Date.now();
const pageUrl = new URL(input.url);
const errors = [], consoleErrors = [], written = [], missing = [], outside = [], unknown = [];
const timers = [], perFn = new Map(), seen = new Set(), rejected = new Map();
let timerId = 0, clock = 0, phase = 'loading', parsedUpTo = Infinity;
let sandbox, VmPromise, vmJSON, root, head, body;
// ここで代わりを置いていない、ブラウザーの名前(使っていても、ページの誤りとはしない)
const BROWSER = new Set(('Worker SharedWorker BroadcastChannel FileReader Blob File FormData indexedDB caches PushManager '
  + 'MediaRecorder AudioContext webkitAudioContext OffscreenCanvas ImageData Path2D DOMParser XMLSerializer Range getSelection '
  + 'Node Element HTMLElement HTMLCanvasElement HTMLImageElement HTMLInputElement HTMLDivElement SVGElement NodeList '
  + 'HTMLCollection DocumentFragment ShadowRoot customElements CSS CSSStyleSheet visualViewport chrome webkit opener '
  + 'PerformanceObserver DeviceOrientationEvent DeviceMotionEvent TouchEvent PointerEvent MouseEvent KeyboardEvent FocusEvent '
  + 'InputEvent UIEvent WheelEvent ErrorEvent MessageEvent ProgressEvent PopStateEvent HashChangeEvent StorageEvent '
  + 'TransitionEvent AnimationEvent webkitSpeechRecognition SpeechRecognition MediaSource RTCPeerConnection '
  + 'WebGLRenderingContext WebGL2RenderingContext Option Video DOMRect DOMMatrix FontFace screenLeft screenTop').split(' '));

function blank() {  // 何をしてもエラーにならない代わりのもの
  const target = function () {};
  return new Proxy(target, {
    get(t, k) {
      if (k === Symbol.toPrimitive) return () => '';
      if (typeof k === 'symbol' || k === 'then') return undefined;
      if (k === 'length') return 0;
      if (k === 'toString' || k === 'valueOf' || k === 'toJSON') return () => '';
      return blank();
    },
    apply() { return blank(); },
    construct() { return blank(); },
    set() { return true; },
    deleteProperty() { return true; },
  });
}
function lenient(own) {
  return new Proxy(own, {
    get(t, k) { if (k in t) return t[k]; if (typeof k === 'symbol') return undefined; return blank(); },
    set(t, k, v) { try { t[k] = v; } catch (e) { /* 読むだけの項目 */ } return true; },
  });
}

const byId = new Map(), byClass = new Map(), byTag = new Map();
function add(map, key, el) { if (!key) return; const list = map.get(key) || []; if (!list.includes(el)) list.push(el); map.set(key, list); }
function textOf(html) {
  return String(html).replace(/<br\s*\/?>/gi, ' ').replace(/<[^>]*>/g, ' ').replace(/&nbsp;/g, ' ').replace(/&deg;|&#176;/g, '°')
    .replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'")
    .replace(/\s+/g, ' ').trim();
}
const attrRe = name => new RegExp('(?:^|\\s)' + name + '\\s*=\\s*(?:"([^"]*)"|\'([^\']*)\'|([^\\s>]+))', 'i');
const ID_ATTR = attrRe('id'), CLASS_ATTR = attrRe('class');
function attr(re, text) { const m = re.exec(text); return m ? (m[1] ?? m[2] ?? m[3] ?? '') : ''; }
const TAG = /<([a-zA-Z][\w-]*)((?:[^>"']|"[^"]*"|'[^']*')*)>([^<]*)/g;
function scan(html, indexed) {  // HTML の中の要素を作る(位置は、元の HTML の中の位置のまま)
  const blankOut = m => ' '.repeat(m.length);
  const markup = String(html).replace(/<!--[\s\S]*?-->/g, blankOut).replace(/<script\b[\s\S]*?<\/script\s*>/gi, blankOut)
    .replace(/<style\b[\s\S]*?<\/style\s*>/gi, blankOut);
  const made = [];
  for (const m of markup.matchAll(TAG)) {
    made.push(element(m[1], attr(ID_ATTR, m[2]).trim(), attr(CLASS_ATTR, m[2]).split(/\s+/).filter(Boolean), textOf(m[3] || ''),
      indexed, '', indexed ? m.index : -1));
  }
  return made;
}

function element(tag, id, classes, text, indexed = true, label = '', pos = -1) {
  const state = { tag: String(tag || 'div').toLowerCase(), id: String(id || ''), classes: new Set(classes || []),
    text: String(text || ''), html: null, attrs: {}, children: [], pos };
  const style = new Proxy({ cssText: '', setProperty(k, v) { this[k] = v; }, removeProperty() { return ''; },
    getPropertyValue(k) { return this[k] || ''; } },
  { get: (t, k) => (k in t ? t[k] : typeof k === 'string' ? '' : undefined), set: (t, k, v) => { t[k] = v; return true; } });
  const classList = {
    add: (...c) => { c.forEach(x => state.classes.add(String(x))); register(); },
    remove: (...c) => { c.forEach(x => state.classes.delete(String(x))); },
    toggle: (c, force) => { const on = force === undefined ? !state.classes.has(String(c)) : !!force;
      if (on) state.classes.add(String(c)); else state.classes.delete(String(c)); register(); return on; },
    contains: c => state.classes.has(String(c)),
    replace: (a, b) => { const had = state.classes.delete(String(a)); if (had) state.classes.add(String(b)); register(); return had; },
    item: i => [...state.classes][i] ?? null,
    forEach: fn => [...state.classes].forEach(fn),
    get length() { return state.classes.size; },
    get value() { return [...state.classes].join(' '); },
  };
  const entry = { state, label };
  const wrote = () => { if (!written.includes(entry)) written.push(entry); };
  const own = {
    nodeType: 1, dataset: {}, scrollTop: 0, scrollLeft: 0, classList, style,
    get tagName() { return state.tag.toUpperCase(); }, get nodeName() { return state.tag.toUpperCase(); },
    get localName() { return state.tag; },
    get id() { return state.id; }, set id(v) { state.id = String(v); register(); },
    get className() { return [...state.classes].join(' '); },
    set className(v) { state.classes = new Set(String(v).split(/\s+/).filter(Boolean)); register(); },
    get textContent() { return state.text; }, set textContent(v) { state.text = v == null ? '' : String(v); state.html = null; wrote(); },
    get innerText() { return state.text; }, set innerText(v) { own.textContent = v; },
    get innerHTML() { return state.html ?? state.text; },
    set innerHTML(v) { state.html = v == null ? '' : String(v); state.text = textOf(state.html); state.children = scan(state.html, false); wrote(); },
    get outerHTML() { return state.html ?? state.text; }, set outerHTML(v) { own.innerHTML = v; },
    get value() { return state.attrs.value ?? ''; }, set value(v) { state.attrs.value = String(v); },
    get src() { return state.attrs.src ?? ''; }, set src(v) { state.attrs.src = String(v); },
    get href() { return state.attrs.href ?? ''; }, set href(v) { state.attrs.href = String(v); },
    get hidden() { return !!state.attrs.hidden; }, set hidden(v) { state.attrs.hidden = !!v; },
    get disabled() { return !!state.attrs.disabled; }, set disabled(v) { state.attrs.disabled = !!v; },
    get checked() { return !!state.attrs.checked; }, set checked(v) { state.attrs.checked = !!v; },
    get children() { return state.children.slice(); }, get childNodes() { return state.children.slice(); },
    get childElementCount() { return state.children.length; },
    get firstChild() { return state.children[0] ?? null; }, get firstElementChild() { return state.children[0] ?? null; },
    get lastChild() { return state.children[state.children.length - 1] ?? null; },
    get lastElementChild() { return state.children[state.children.length - 1] ?? null; },
    get parentNode() { return body; }, get parentElement() { return body; },
    get nextElementSibling() { return null; }, get previousElementSibling() { return null; },
    get nextSibling() { return null; }, get previousSibling() { return null; },
    get offsetWidth() { return 1920; }, get offsetHeight() { return 1080; }, get clientWidth() { return 1920; },
    get clientHeight() { return 1080; }, get scrollWidth() { return 1920; }, get scrollHeight() { return 1080; },
    setAttribute(k, v) { k = String(k).toLowerCase(); if (k === 'id') own.id = v; else if (k === 'class') own.className = v; else state.attrs[k] = String(v); },
    getAttribute(k) { k = String(k).toLowerCase(); if (k === 'id') return state.id || null; if (k === 'class') return own.className || null;
      return k in state.attrs ? String(state.attrs[k]) : null; },
    hasAttribute(k) { return own.getAttribute(k) != null; },
    removeAttribute(k) { delete state.attrs[String(k).toLowerCase()]; },
    toggleAttribute(k, force) { const on = force === undefined ? !own.hasAttribute(k) : !!force;
      if (on) state.attrs[String(k).toLowerCase()] = ''; else own.removeAttribute(k); return on; },
    appendChild(c) { state.children.push(c); return c; },
    append(...c) { for (const x of c) { if (x && typeof x === 'object') state.children.push(x); else if (x != null) { state.text += String(x); wrote(); } } },
    prepend(...c) { for (const x of c) { if (x && typeof x === 'object') state.children.unshift(x); else if (x != null) { state.text = String(x) + state.text; wrote(); } } },
    insertBefore(c) { state.children.push(c); return c; },
    replaceChild(n, o) { const i = state.children.indexOf(o); if (i >= 0) state.children[i] = n; return o; },
    removeChild(c) { const i = state.children.indexOf(c); if (i >= 0) state.children.splice(i, 1); return c; },
    replaceChildren(...c) { state.children = c.filter(x => x && typeof x === 'object');
      state.text = c.map(x => (x && typeof x === 'object' ? String(x.textContent ?? '') : String(x ?? ''))).join(''); wrote(); },
    remove() {}, before() {}, after() {}, replaceWith() {},
    insertAdjacentHTML(_, h) { state.children.push(...scan(String(h), false)); state.text += textOf(String(h)); wrote(); },
    insertAdjacentText(_, t) { state.text += String(t); wrote(); },
    insertAdjacentElement(_, e) { state.children.push(e); return e; },
    addEventListener() {}, removeEventListener() {}, dispatchEvent() { return true; },
    querySelector: s => find(s), querySelectorAll: s => findAll(s),
    getElementsByClassName: c => (byClass.get(String(c)) || []).slice(),
    getElementsByTagName: t => (byTag.get(String(t).toLowerCase()) || []).slice(),
    closest: () => element('div', '', [], '', false), matches: () => false, contains: () => true,
    getBoundingClientRect: () => ({ x: 0, y: 0, top: 0, left: 0, right: 1920, bottom: 1080, width: 1920, height: 1080 }),
    getClientRects: () => [], getContext: () => blank(), animate: () => blank(),
    cloneNode() { return element(state.tag, '', [...state.classes], state.text, false); },
    focus() {}, blur() {}, click() {}, scrollIntoView() {}, scrollTo() {}, select() {}, pause() {}, load() {},
    requestFullscreen: () => VmPromise.resolve(), play: () => VmPromise.resolve(),
  };
  const el = lenient(own);
  entry.el = el;
  state.el = el;
  function register() { if (state.id) byId.set(state.id, el); for (const c of state.classes) add(byClass, c, el); }
  if (indexed) add(byTag, state.tag, el);
  register();
  els.set(el, state);
  return el;
}
const els = new WeakMap();
const visible = el => { const s = els.get(el); return !s || s.pos < parsedUpTo; };  // その時点で、もう読み込まれている要素か

function query(sel) {  // #id・.class・タグ(と、その組み合わせ)だけを読む。ほかの書き方は null(読まない)
  sel = String(sel ?? '').trim();
  if (!sel || /[,\[\]:*()|^$"'=]/.test(sel)) return null;
  const last = sel.split(/\s*[>+~]\s*|\s+/).filter(Boolean).pop() || '';
  const m = /^([a-zA-Z][\w-]*)?((?:[#.][\w-]+)*)$/.exec(last);
  if (!m || (!m[1] && !m[2])) return null;
  const parts = m[2].match(/[#.][\w-]+/g) || [];
  return { tag: m[1] ? m[1].toLowerCase() : '', ids: parts.filter(p => p[0] === '#').map(p => p.slice(1)),
    classes: parts.filter(p => p[0] === '.').map(p => p.slice(1)) };
}
function matching(q, all) {
  const pool = q.ids.length ? (byId.has(q.ids[0]) ? [byId.get(q.ids[0])] : []) : q.classes.length ? (byClass.get(q.classes[0]) || [])
    : (byTag.get(q.tag) || []);
  return pool.filter(el => (!q.tag || el.localName === q.tag) && q.classes.every(c => el.classList.contains(c))
    && (!q.ids.length || el.id === q.ids[0]) && (all || visible(el)));
}
function lost(sel, q) { missing.push({ sel: String(sel).trim(), later: matching(q, true).length > 0 }); return null; }
function find(sel) {
  const q = query(sel);
  if (!q) return element('div', '', [], '', false, String(sel ?? '').trim());  // 読まない書き方: 見つかったことにする
  if (!q.ids.length && !q.classes.length && ['html', 'head', 'body'].includes(q.tag)) return q.tag === 'html' ? root : q.tag === 'head' ? head : body;
  return matching(q, false)[0] || lost(sel, q);
}
function findAll(sel) { const q = query(sel); return q ? matching(q, false).slice() : []; }

const docListeners = {}, winListeners = {};
function on(store, type, fn) { if (typeof fn === 'function' || (fn && typeof fn.handleEvent === 'function')) (store[type] = store[type] || []).push(fn); }
function makeEvent(type, target) {
  return { type, target, currentTarget: target, timeStamp: Date.now() - started, isTrusted: true, defaultPrevented: false,
    preventDefault() {}, stopPropagation() {}, stopImmediatePropagation() {} };
}
function fire(store, type, target) {
  for (const fn of store[type] || []) call(typeof fn === 'function' ? fn : fn.handleEvent.bind(fn), [makeEvent(type, target)]);
}
const docOwn = {
  nodeType: 9, title: '', cookie: '', referrer: '', hidden: false, visibilityState: 'visible', characterSet: 'UTF-8',
  fullscreenElement: null, activeElement: null,
  getElementById(id) { const el = byId.get(String(id)); if (el && visible(el)) return el;
    missing.push({ sel: '#' + id, later: !!el }); return null; },
  querySelector: s => find(s), querySelectorAll: s => findAll(s),
  getElementsByClassName: c => (byClass.get(String(c)) || []).filter(visible),
  getElementsByTagName: t => (byTag.get(String(t).toLowerCase()) || []).filter(visible),
  getElementsByName: () => [],
  createElement: t => element(t, '', [], '', false), createElementNS: (_, t) => element(t, '', [], '', false),
  createTextNode: t => ({ nodeType: 3, textContent: String(t), nodeValue: String(t) }),
  createDocumentFragment: () => element('fragment', '', [], '', false), createComment: () => ({ nodeType: 8 }),
  addEventListener: (type, fn) => on(docListeners, type, fn), removeEventListener() {}, dispatchEvent: () => true,
  hasFocus: () => true, execCommand: () => false,
  exitFullscreen: () => VmPromise.resolve(),
  get readyState() { return phase; },
  get body() { return body; }, get head() { return head; }, get documentElement() { return root; },
  get location() { return sandbox.location; },
  get fonts() { return { ready: VmPromise.resolve(), load: () => VmPromise.resolve([]), check: () => true, addEventListener() {} }; },
};
const documentObj = lenient(docOwn);

function addTimer(fn, ms, args, every) {
  if (typeof fn !== 'function') return 0;
  const delay = Math.max(Number(ms) || 0, 0);
  timers.push({ id: ++timerId, fn, delay, args, every, due: clock + (every ? Math.max(delay, 1) : delay), runs: 0 });
  return timerId;
}
function clear(id) { const i = timers.findIndex(t => t.id === id); if (i >= 0) timers.splice(i, 1); }
function call(fn, args) { try { fn.apply(sandbox, args || []); } catch (e) { report(e); } }
function report(e) {
  const isObj = e !== null && typeof e === 'object';
  const name = isObj && e.name ? String(e.name) : 'Error';
  const message = isObj && 'message' in e ? String(e.message) : String(e);
  if (name === 'ReferenceError') {
    const m = /^(\S+) is not defined/.exec(message);
    if (m && (input.outside || BROWSER.has(m[1]))) return;  // 外のファイル(CDN など)が作る名前か、ここで代わりを置いていない機能
  }
  const at = /page-script-(\d+)\.js:(\d+)/.exec(isObj && e.stack ? String(e.stack) : '');
  const key = name + ':' + message + ':' + (at ? at[0] : '');
  if (seen.has(key)) return;
  seen.add(key);
  errors.push({ name, message: message.slice(0, 300), script: at ? Number(at[1]) : 0, line: at ? Number(at[2]) : 0 });
}
function show(v) { try { return typeof v === 'string' ? v : v && v.message ? String(v.message) : JSON.stringify(v); } catch (e) { return String(v); } }
function storage() {
  const items = new Map();
  return { getItem: k => (items.has(String(k)) ? items.get(String(k)) : null), setItem: (k, v) => { items.set(String(k), String(v)); },
    removeItem: k => { items.delete(String(k)); }, clear: () => items.clear(), key: i => [...items.keys()][i] ?? null,
    get length() { return items.size; } };
}
function response(status, text) {
  return { ok: status >= 200 && status < 300, status, statusText: status === 200 ? 'OK' : '', redirected: false, type: 'basic', url: '',
    headers: { get: k => (String(k).toLowerCase() === 'content-type' ? 'application/json' : null), has: () => false, forEach() {} },
    json: () => new VmPromise((resolve, reject) => { try { resolve(vmJSON.parse(text)); } catch (e) { reject(e); } }),
    text: () => VmPromise.resolve(text), blob: () => VmPromise.resolve(blank()), arrayBuffer: () => VmPromise.resolve(new ArrayBuffer(0)),
    clone() { return response(status, text); } };
}
function fetchMock(resource) {
  let url;
  try { url = new URL(typeof resource === 'string' ? resource : (resource && resource.url) || String(resource), pageUrl.href); }
  catch (e) { return VmPromise.reject(new TypeError('Failed to fetch')); }
  if (url.origin !== pageUrl.origin) { outside.push(url.origin); return new VmPromise(() => {}); }  // 外のサイトは開かない
  const reply = input.replies[url.pathname];
  if (!reply) { unknown.push(url.pathname); return new VmPromise(() => {}); }  // 確かめで開いていない URL: 答えが返らないまま
  return VmPromise.resolve(response(reply[0], reply[1]));
}
class Observer { observe() {} unobserve() {} disconnect() {} takeRecords() { return []; } }

async function main() {
  const loc = { href: pageUrl.href, origin: pageUrl.origin, protocol: pageUrl.protocol, host: pageUrl.host, hostname: pageUrl.hostname,
    port: pageUrl.port, pathname: pageUrl.pathname, search: pageUrl.search, hash: '', reload() {}, assign() {}, replace() {},
    toString() { return pageUrl.href; } };
  sandbox = {
    document: documentObj, location: loc, name: '', status: '', origin: pageUrl.origin, event: undefined, isSecureContext: false,
    console: { log() {}, info() {}, debug() {}, warn() {}, table() {}, trace() {}, group() {}, groupCollapsed() {}, groupEnd() {},
      time() {}, timeEnd() {}, timeLog() {}, count() {}, assert() {}, dir() {}, clear() {},
      error: (...a) => { consoleErrors.push(a.map(show).join(' ').slice(0, 200)); } },
    setTimeout: (fn, ms, ...args) => addTimer(fn, ms, args, false), setInterval: (fn, ms, ...args) => addTimer(fn, ms, args, true),
    clearTimeout: clear, clearInterval: clear,
    requestAnimationFrame: fn => addTimer(() => fn(Date.now() - started), 16, [], false), cancelAnimationFrame: clear,
    requestIdleCallback: fn => addTimer(() => fn({ timeRemaining: () => 50, didTimeout: false }), 1, [], false), cancelIdleCallback: clear,
    queueMicrotask, structuredClone, URL, URLSearchParams, AbortController, AbortSignal, TextEncoder, TextDecoder, atob, btoa,
    Event, EventTarget, CustomEvent: globalThis.CustomEvent || Event, crypto: globalThis.crypto,
    fetch: fetchMock, Headers: class { get() { return null; } set() {} append() {} has() { return false; } delete() {} forEach() {} },
    XMLHttpRequest: function () { return blank(); }, WebSocket: function () { return blank(); }, EventSource: function () { return blank(); },
    Image: function () { return element('img', '', [], '', false); }, Audio: function () { return element('audio', '', [], '', false); },
    IntersectionObserver: Observer, ResizeObserver: Observer, MutationObserver: Observer,
    Notification: Object.assign(function () { return blank(); }, { permission: 'default', requestPermission: () => VmPromise.resolve('default') }),
    speechSynthesis: blank(), SpeechSynthesisUtterance: function () { return blank(); },
    navigator: lenient({ userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0', language: 'ja-JP', languages: ['ja-JP', 'ja'],
      onLine: true, platform: 'Win32', hardwareConcurrency: 8, maxTouchPoints: 0, cookieEnabled: true,
      geolocation: { getCurrentPosition() {}, watchPosition() { return 0; }, clearWatch() {} },
      clipboard: { writeText: () => VmPromise.resolve(), readText: () => VmPromise.resolve('') } }),
    history: { pushState() {}, replaceState() {}, back() {}, forward() {}, go() {}, length: 1, state: null },
    localStorage: storage(), sessionStorage: storage(),
    alert() {}, confirm: () => false, prompt: () => null, open: () => null, close() {}, print() {}, focus() {}, blur() {},
    scrollTo() {}, scrollBy() {}, scroll() {}, postMessage() {}, dispatchEvent: () => true,
    addEventListener: (type, fn) => on(winListeners, type, fn), removeEventListener() {},
    matchMedia: q => ({ matches: false, media: String(q), onchange: null, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }),
    getComputedStyle: el => (el && el.style) || {},
    innerWidth: 1920, innerHeight: 1080, outerWidth: 1920, outerHeight: 1080, devicePixelRatio: 1, scrollX: 0, scrollY: 0,
    pageXOffset: 0, pageYOffset: 0,
    screen: { width: 1920, height: 1080, availWidth: 1920, availHeight: 1080, colorDepth: 24,
      orientation: { type: 'landscape-primary', angle: 0, addEventListener() {}, lock: () => VmPromise.resolve() } },
    performance: { now: () => Date.now() - started, mark() {}, measure() {}, getEntriesByType: () => [], timeOrigin: started },
  };
  sandbox.window = sandbox; sandbox.self = sandbox; sandbox.top = sandbox; sandbox.parent = sandbox; sandbox.frames = sandbox;
  const context = vm.createContext(sandbox);
  VmPromise = vm.runInContext('Promise', context);
  vmJSON = vm.runInContext('JSON', context);
  process.on('unhandledRejection', (reason, promise) => { rejected.set(promise, reason); });
  process.on('rejectionHandled', promise => { rejected.delete(promise); });
  scan(input.html, true);
  root = (byTag.get('html') || [])[0] || element('html', '', [], '', false);
  head = (byTag.get('head') || [])[0] || element('head', '', [], '', false);
  body = (byTag.get('body') || [])[0] || element('body', '', [], '', false);
  for (const [id, el] of byId) {  // ブラウザーでは、id の付いた要素は、その名前の変数としても使える(<div id="clock"> → clock)
    if (/^[A-Za-z_$][\w$]*$/.test(id) && !(id in sandbox)) sandbox[id] = el;
  }
  const settle = async (rounds = 8) => { for (let i = 0; i < rounds; i++) await new Promise(r => setImmediate(r)); };
  for (const [i, script] of input.scripts.entries()) {
    parsedUpTo = script.at;  // この <script> より後ろの要素は、まだ読み込まれていない
    try { vm.runInContext(script.code, context, { filename: `page-script-${i + 1}.js`, timeout: 4000 }); } catch (e) { report(e); }
  }
  parsedUpTo = Infinity;
  await settle();
  phase = 'interactive';
  fire(docListeners, 'readystatechange', documentObj);
  fire(docListeners, 'DOMContentLoaded', documentObj);
  await settle();
  phase = 'complete';
  fire(docListeners, 'readystatechange', documentObj);
  fire(winListeners, 'load', sandbox);
  if (typeof sandbox.onload === 'function') call(sandbox.onload, [makeEvent('load', sandbox)]);
  fire(winListeners, 'pageshow', sandbox);
  await settle();
  let calls = 0;
  while (calls < 40 && Date.now() - started < 8000) {  // タイマー: 早いものから、それぞれ2回まで(同じ関数は3回まで)
    const next = timers.filter(t => t.runs < 2).sort((a, b) => a.due - b.due || a.id - b.id)[0];
    if (!next) break;
    const count = perFn.get(next.fn) || 0;
    if (count >= 3) { clear(next.id); continue; }
    perFn.set(next.fn, count + 1);
    clock = next.due;
    next.runs++;
    calls++;
    if (next.every) next.due += Math.max(next.delay, 1); else clear(next.id);
    call(next.fn, next.args);
    await settle();
  }
  await settle();
  for (const reason of rejected.values()) report(reason);
  const name = s => s.label || (s.id ? '#' + s.id : s.classes.size ? '.' + [...s.classes][0] : s.tag);
  const lostOnes = [];
  for (const m of missing) if (!lostOnes.some(x => x.sel === m.sel)) lostOnes.push(m);
  fs.writeFileSync(process.argv[3], JSON.stringify({
    errors: errors.slice(0, 6), texts: written.map(({ state }) => [name(state), textOf(state.html ?? state.text).slice(0, 120)]).slice(0, 30),
    missing: lostOnes.slice(0, 10), console: consoleErrors.slice(0, 5), outside: [...new Set(outside)].slice(0, 5),
    unknown: [...new Set(unknown)].slice(0, 5), timers: calls }));
}
main().then(() => process.exit(0), e => { fs.writeFileSync(process.argv[3], JSON.stringify({ crashed: String((e && e.stack) || e) })); process.exit(0); });
"""


def run_page(url: str, html: str, scripts: list[tuple[int, str]], replies: dict[str, tuple[int, str]],
             outside: bool = False, timeout: float = 20.0) -> dict | None:
    """ページの JavaScript を node で動かした結果。node が無い・動かす JavaScript が無い・動かせなければ None。

    scripts は、ページの順の (HTML の中の <script> の位置, 中身)(後回しにする defer の外のファイルは、位置を HTML の長さに)。
    replies は、同じサーバーの道のり → (状態の番号, 本文)。outside は、外のサイトの JavaScript を読み込むページか
    (その名前は分からないので、定義されていない名前を誤りとしない)。
    戻り値: {"errors": [{name, message, script, line}], "texts": [[要素, 表示]], "missing": [{sel, later}], ...}
    """
    node = _node()
    if not node or not any(code.strip() for _, code in scripts):
        return None
    with tempfile.TemporaryDirectory(prefix="celestial-page-") as tmp:
        runner, data, out = Path(tmp) / "run.js", Path(tmp) / "page.json", Path(tmp) / "out.json"
        runner.write_text(_RUNNER, encoding="utf-8")
        data.write_text(json.dumps({"url": url, "html": html, "scripts": [{"at": at, "code": code} for at, code in scripts],
                                    "replies": replies, "outside": outside}, ensure_ascii=False), encoding="utf-8")
        try:
            subprocess.run([node, str(runner), str(data), str(out)], capture_output=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), cwd=tmp,
                           env={**os.environ, "NODE_OPTIONS": ""})
            result = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError, subprocess.SubprocessError):
            return None
    return None if "crashed" in result else result


_NUMBER_SHOWN = re.compile(r"(?:[^\d\s]{1,10}\s*[:：]\s*)?(\d{1,2})")
_WEATHER_LABEL = re.compile(r"weather|tenki|condition|天気|sky|forecast|desc", re.I)
_OTHER_LABEL = re.compile(r"temp|humid|wind|rain|precip|max|min|high|low|feels|press|uv|code|icon|気温|湿度|風|降水", re.I)
_BROKEN_TEXT = re.compile(r"\bundefined\b|\bNaN\b|\[object Object\]")
_LOOKUP = re.compile(r"""(?:getElementById\(\s*['"]([\w-]+)['"]|querySelector(?:All)?\(\s*['"]([^'"]+)['"])""")
PAGE_RAN = "ページの JavaScript を動かすと"  # 動かして分かった誤りの見出し(エージェントが、直し方の知らせを選ぶのに使う)
WEATHER_TEXTS = ("0 快晴、1 晴れ、2 晴れ時々曇り、3 曇り、45/48 霧、51-57 霧雨、61-67 雨、71-77 雪、80-82 にわか雨、95-99 雷雨")


# 天気の欄に出ると誤りの文字(「不明」など。読み込み中の「--」「?」は、外のサービスが答えないあいだにも出るので見ない)
_UNKNOWN_SHOWN = re.compile(r"(?:[^\s:：]{1,10}\s*[:：]\s*)?(?:不明|unknown|n/a|null|none)", re.I)


def describe(run: dict, codes: list[str]) -> tuple[list[str], list[str]]:
    """動かした結果を、AIに渡す (知らせ, 誤り) の文にする。codes は、動かした JavaScript(run_page に渡した順)。"""
    notes: list[str] = []
    problems: list[str] = []
    missing = run.get("missing") or []
    for error in (run.get("errors") or [])[:3]:
        where, source = "", ""
        index, line = int(error.get("script") or 0), int(error.get("line") or 0)
        if 0 < index <= len(codes) and line > 0:
            lines = codes[index - 1].split("\n")
            if line <= len(lines):
                source = lines[line - 1].strip()
                where = f"(<script> の中の {line} 行目: {source[:160]})"
        text = f"{PAGE_RAN}、エラーで止まります: {error.get('name')}: {error.get('message')}{where}"
        if "null" in str(error.get("message")) and missing:
            asked = {a or b for a, b in _LOOKUP.findall(source)} if source else set()
            near = [m for m in missing if not asked or m["sel"] in asked or m["sel"].lstrip("#") in asked] or missing
            hit = near[0]
            if hit.get("later"):
                text += (f"。この <script> は {hit['sel']} の要素より前(head の中など)にあり、動く時点では、まだその要素がありません:"
                         " <script> を </body> の直前に移すか、document.addEventListener('DOMContentLoaded', () => { ... }) の中で"
                         "動かしてください")
            else:
                text += (f"。ページに {hit['sel']} の要素がありません: HTML にその要素を置くか、JavaScript で探す名前を HTML の "
                         "id・class に合わせてください")
        problems.append(text)
    texts = [(str(name), str(shown)) for name, shown in (run.get("texts") or [])]
    if texts:
        notes.append("- ページの JavaScript を動かしたときの表示: " + " / ".join(f"{name}「{shown[:60]}」" for name, shown in texts[:8]))
    for name, shown in texts:
        number = _NUMBER_SHOWN.fullmatch(shown.strip())
        if number and _WEATHER_LABEL.search(name) and not _OTHER_LABEL.search(name):
            problems.append(f"{PAGE_RAN}、天気の欄({name})に、番号「{number.group(1)}」がそのまま出ます(Open-Meteo の "
                            f"weather_code は天気の番号です)。{WEATHER_TEXTS} のように、文字にして表示してください")
            break
    for name, shown in texts:
        if _UNKNOWN_SHOWN.fullmatch(shown.strip()) and _WEATHER_LABEL.search(name) and not _OTHER_LABEL.search(name):
            # サーバーは答えたが、天気が入っていない(URL で weather_code を頼まず、KeyError を「不明」にしていた。実際にあった)
            problems.append(f"{PAGE_RAN}、天気の欄({name})に「{shown.strip()[:20]}」と出ます(サーバーは答えていますが、天気が"
                            "入っていません)。Open-Meteo なら、URL の current= に weather_code があるか(無ければ足す)と、番号を"
                            f"文字にする表にその番号があるか({WEATHER_TEXTS})を確かめてください")
            break
    for name, shown in texts:
        if _BROKEN_TEXT.search(shown):
            problems.append(f"{PAGE_RAN}、{name} に「{shown[:80]}」と出ます(undefined や NaN は、読んでいる項目が答えに無いか、"
                            "名前が違うときに出ます): サーバーが返す JSON の項目の名前と、ページが読む名前を合わせてください")
            break
    return notes, problems
