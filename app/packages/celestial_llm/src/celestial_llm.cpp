// Celestial のスマホ内AI: llama.cpp を Dart から使うための小さな C インターフェース。
//
// 使い方(Dart 側は1つの isolate から順に呼ぶ。cl_stop だけは別の isolate から呼んでよい):
//   h = cl_load(path, n_ctx, threads, threads_batch, err, len)
//   n = cl_start(h, roles, contents, count, temp, top_p, min_p, repeat_penalty, max_tokens, seed, err, len)
//   while ((k = cl_next(h, buf, len)) > 0) { ... buf の k バイト(UTF-8 の断片)... }
//   cl_free(h)
//
// 速さのための工夫: 直前の会話と先頭が同じ部分は KV キャッシュを使い回し、新しい部分だけを計算する。

#include "llama.h"

#include <algorithm>
#include <atomic>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <vector>

#if defined(_WIN32)
#define CL_EXPORT extern "C" __declspec(dllexport)
#else
#define CL_EXPORT extern "C" __attribute__((visibility("default"))) __attribute__((used))
#endif

namespace {

struct Session {
    llama_model * model = nullptr;
    llama_context * ctx = nullptr;
    const llama_vocab * vocab = nullptr;
    llama_sampler * sampler = nullptr;
    std::vector<llama_token> cached;  // いま KV キャッシュに入っているトークン(系列 0)
    std::atomic<bool> stop{false};
    std::string tmpl;
    int n_ctx = 0;
    int n_batch = 512;
    int max_tokens = 0;
    int n_generated = 0;
    int n_prompt = 0;
    int n_reused = 0;
};

std::once_flag g_init;

void quiet_log(enum ggml_log_level, const char *, void *) {}

void set_error(char * err, int len, const char * message) {
    if (err && len > 0) {
        std::snprintf(err, (size_t) len, "%s", message);
    }
}

bool abort_cb(void * data) {
    return static_cast<Session *>(data)->stop.load();
}

// 組み込みのテンプレートに当てはまらないモデル向けの ChatML(Qwen など多くのモデルが使う形式)
std::string chatml(const std::vector<llama_chat_message> & msgs) {
    std::string out;
    for (const auto & m : msgs) {
        out += "<|im_start|>";
        out += m.role;
        out += "\n";
        out += m.content;
        out += "<|im_end|>\n";
    }
    out += "<|im_start|>assistant\n";
    return out;
}

}  // namespace

CL_EXPORT int cl_version(void) { return 2; }

CL_EXPORT void * cl_load(const char * path, int n_ctx, int n_threads, int n_threads_batch, char * err, int err_len) {
    std::call_once(g_init, [] {
        llama_log_set(quiet_log, nullptr);
        llama_backend_init();
    });
    llama_model_params mparams = llama_model_default_params();
    mparams.n_gpu_layers = 0;
    llama_model * model = llama_model_load_from_file(path, mparams);
    if (!model) {
        set_error(err, err_len, "model load failed");
        return nullptr;
    }
    llama_context_params cparams = llama_context_default_params();
    cparams.n_ctx = (uint32_t) std::max(512, n_ctx);
    cparams.n_batch = 512;
    cparams.n_ubatch = 512;
    cparams.n_threads = std::max(1, n_threads);
    cparams.n_threads_batch = std::max(1, n_threads_batch);
    cparams.no_perf = true;
    llama_context * ctx = llama_init_from_model(model, cparams);
    if (!ctx) {
        llama_model_free(model);
        set_error(err, err_len, "context init failed (not enough memory?)");
        return nullptr;
    }
    auto * s = new Session();
    s->model = model;
    s->ctx = ctx;
    s->vocab = llama_model_get_vocab(model);
    s->n_ctx = (int) llama_n_ctx(ctx);
    const char * t = llama_model_chat_template(model, nullptr);
    if (t) {
        s->tmpl = t;
    }
    llama_set_abort_callback(ctx, abort_cb, s);
    return s;
}

CL_EXPORT void cl_free(void * handle) {
    auto * s = static_cast<Session *>(handle);
    if (!s) {
        return;
    }
    if (s->sampler) {
        llama_sampler_free(s->sampler);
    }
    llama_free(s->ctx);
    llama_model_free(s->model);
    delete s;
}

CL_EXPORT void cl_stop(void * handle) {
    auto * s = static_cast<Session *>(handle);
    if (s) {
        s->stop.store(true);
    }
}

CL_EXPORT void cl_reset(void * handle) {
    auto * s = static_cast<Session *>(handle);
    if (!s) {
        return;
    }
    llama_memory_clear(llama_get_memory(s->ctx), true);
    s->cached.clear();
}

// {"desc":..., "params":..., "size":..., "n_ctx":..., "template":..., "n_prompt":..., "n_reused":..., "n_generated":...}
CL_EXPORT int cl_info(void * handle, char * buf, int len) {
    auto * s = static_cast<Session *>(handle);
    if (!s || !buf || len <= 0) {
        return -1;
    }
    char desc[256] = {0};
    llama_model_desc(s->model, desc, sizeof(desc));
    for (char * p = desc; *p; ++p) {
        if (*p == '"' || *p == '\\') {
            *p = ' ';
        }
    }
    const char * kind = s->tmpl.empty() ? "chatml" : (s->tmpl.find("<start_of_turn>") != std::string::npos ? "gemma" : "model");
    return std::snprintf(buf, (size_t) len,
                         "{\"desc\":\"%s\",\"params\":%llu,\"size\":%llu,\"n_ctx\":%d,\"template\":\"%s\","
                         "\"n_prompt\":%d,\"n_reused\":%d,\"n_generated\":%d}",
                         desc, (unsigned long long) llama_model_n_params(s->model), (unsigned long long) llama_model_size(s->model),
                         s->n_ctx, kind, s->n_prompt, s->n_reused, s->n_generated);
}

// 会話を受け取って、回答の生成を始める準備をする。戻り値: 入力のトークン数(負なら失敗、-2 は停止)
CL_EXPORT int cl_start(void * handle, const char ** roles, const char ** contents, int count,
                       float temp, float top_p, float min_p, float repeat_penalty, int max_tokens, unsigned int seed,
                       char * err, int err_len) {
    auto * s = static_cast<Session *>(handle);
    if (!s || count <= 0) {
        set_error(err, err_len, "invalid arguments");
        return -1;
    }
    s->stop.store(false);
    std::vector<llama_chat_message> msgs((size_t) count);
    size_t total = 0;
    for (int i = 0; i < count; ++i) {
        msgs[(size_t) i] = {roles[i], contents[i]};
        total += std::strlen(contents[i]) + 32;
    }

    // 1. モデルの会話形式に整える
    std::string prompt;
    const char * tmpl = s->tmpl.empty() ? nullptr : s->tmpl.c_str();
    if (tmpl) {
        std::vector<char> out(total * 2 + 256);
        int n = llama_chat_apply_template(tmpl, msgs.data(), msgs.size(), true, out.data(), (int32_t) out.size());
        if (n > (int) out.size()) {
            out.resize((size_t) n + 1);
            n = llama_chat_apply_template(tmpl, msgs.data(), msgs.size(), true, out.data(), (int32_t) out.size());
        }
        if (n > 0) {
            prompt.assign(out.data(), (size_t) n);
        }
    }
    if (prompt.empty()) {
        prompt = chatml(msgs);
    }

    // 2. トークンに分ける
    int n_tok = -llama_tokenize(s->vocab, prompt.c_str(), (int32_t) prompt.size(), nullptr, 0, true, true);
    if (n_tok <= 0) {
        set_error(err, err_len, "tokenize failed");
        return -1;
    }
    std::vector<llama_token> tokens((size_t) n_tok);
    if (llama_tokenize(s->vocab, prompt.c_str(), (int32_t) prompt.size(), tokens.data(), n_tok, true, true) < 0) {
        set_error(err, err_len, "tokenize failed");
        return -1;
    }
    const int room = std::max(16, std::min(max_tokens, s->n_ctx / 2));
    if ((int) tokens.size() + room > s->n_ctx) {
        set_error(err, err_len, "conversation is too long for the context");
        return -3;
    }

    // 3. 先頭が同じ部分は KV キャッシュを使い回し、違う部分から先を計算する
    size_t keep = 0;
    while (keep < s->cached.size() && keep < tokens.size() && s->cached[keep] == tokens[keep]) {
        ++keep;
    }
    if (keep == tokens.size()) {
        --keep;  // 最後の1トークンは計算し直して、次の予測を得る
    }
    llama_memory_t mem = llama_get_memory(s->ctx);
    if (!llama_memory_seq_rm(mem, 0, (llama_pos) keep, -1)) {
        llama_memory_clear(mem, true);
        keep = 0;
    }
    s->cached.resize(keep);
    s->n_reused = (int) keep;
    s->n_prompt = (int) tokens.size();
    for (size_t i = keep; i < tokens.size(); i += (size_t) s->n_batch) {
        const int n = (int) std::min((size_t) s->n_batch, tokens.size() - i);
        llama_batch batch = llama_batch_get_one(tokens.data() + i, n);
        const int rc = llama_decode(s->ctx, batch);
        if (s->stop.load()) {
            llama_memory_seq_rm(mem, 0, (llama_pos) s->cached.size(), -1);
            return -2;
        }
        if (rc != 0) {
            llama_memory_clear(mem, true);
            s->cached.clear();
            set_error(err, err_len, "decode failed");
            return -1;
        }
        s->cached.insert(s->cached.end(), tokens.begin() + (std::ptrdiff_t) i, tokens.begin() + (std::ptrdiff_t) i + n);
    }

    // 4. 選び方(サンプラー)
    if (s->sampler) {
        llama_sampler_free(s->sampler);
    }
    llama_sampler_chain_params sp = llama_sampler_chain_default_params();
    sp.no_perf = true;
    s->sampler = llama_sampler_chain_init(sp);
    if (repeat_penalty > 1.0f) {
        llama_sampler_chain_add(s->sampler, llama_sampler_init_penalties(llama_vocab_n_tokens(s->vocab), 64, repeat_penalty, 0.0f, 0.0f));
    }
    if (temp <= 0.0f) {
        llama_sampler_chain_add(s->sampler, llama_sampler_init_greedy());
    } else {
        llama_sampler_chain_add(s->sampler, llama_sampler_init_top_k(40));
        llama_sampler_chain_add(s->sampler, llama_sampler_init_top_p(top_p, 1));
        llama_sampler_chain_add(s->sampler, llama_sampler_init_min_p(min_p, 1));
        llama_sampler_chain_add(s->sampler, llama_sampler_init_temp(temp));
        llama_sampler_chain_add(s->sampler, llama_sampler_init_dist(seed));
    }
    s->max_tokens = std::max(1, max_tokens);
    s->n_generated = 0;
    return (int) tokens.size();
}

// 次の1トークンを作る。戻り値: 書いたバイト数(>0)/ 0 = 終わり / -1 = 失敗 / -2 = 停止
CL_EXPORT int cl_next(void * handle, char * buf, int len) {
    auto * s = static_cast<Session *>(handle);
    if (!s || !s->sampler) {
        return -1;
    }
    if (s->stop.load()) {
        return -2;
    }
    // 文字にならないトークン(特殊トークンなど)は飛ばして、文字が出るまで進める
    while (s->n_generated < s->max_tokens && (int) s->cached.size() < s->n_ctx - 1) {
        llama_token token = llama_sampler_sample(s->sampler, s->ctx, -1);
        if (llama_vocab_is_eog(s->vocab, token)) {
            return 0;
        }
        int n = llama_token_to_piece(s->vocab, token, buf, len, 0, false);
        llama_batch batch = llama_batch_get_one(&token, 1);
        const int rc = llama_decode(s->ctx, batch);
        if (s->stop.load()) {
            return -2;
        }
        if (rc != 0) {
            return -1;
        }
        s->cached.push_back(token);
        s->n_generated++;
        if (n > 0) {
            return n;
        }
    }
    return 0;
}
