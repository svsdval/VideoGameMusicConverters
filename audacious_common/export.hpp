// Экспорт в XM / MIDI из Audacious: пункт меню плейлиста запускает соответствующий
// конвертер репозитория (*2xm-midi-wav.py) для выделенных треков.
// Подключается из каждого плагина; настройки лежат в секции плагина (ключи exp_*).
#pragma once
#include <string>
#include <thread>
#include <vector>

#include <fcntl.h>
#include <signal.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#include <libaudcore/audstrings.h>
#include <libaudcore/i18n.h>
#include <libaudcore/interface.h>
#include <libaudcore/playlist.h>
#include <libaudcore/preferences.h>
#include <libaudcore/runtime.h>

#ifndef EXPORT_SCRIPT_DIR
#define EXPORT_SCRIPT_DIR ""
#endif

// ── виджеты / значения по умолчанию (вставляются в списки плагина) ──
#define EXPORT_WIDGETS(SEC) \
    WidgetLabel(N_("<b>Export to XM / MIDI</b> (playlist menu: right click on tracks)")), \
    WidgetCheck(N_("Write XM (tracker module)"), WidgetBool(SEC, "exp_xm")), \
    WidgetCheck(N_("Write MIDI"), WidgetBool(SEC, "exp_midi")), \
    WidgetCheck(N_("Write LMMS project (.mmpz, uses xm2mmpz.py from the scripts folder)"), WidgetBool(SEC, "exp_lmms")), \
    WidgetSpin(N_("Emulate seconds (0 = automatic):"), WidgetInt(SEC, "exp_seconds"), {0, 3600, 10, N_("s")}), \
    WidgetEntry(N_("Output folder (empty = next to the source):"), WidgetString(SEC, "exp_dir")), \
    WidgetEntry(N_("Converter scripts folder:"), WidgetString(SEC, "exp_scripts")), \
    WidgetEntry(N_("Python command:"), WidgetString(SEC, "exp_python"))

#define EXPORT_DEFAULTS \
    "exp_xm", "TRUE", "exp_midi", "TRUE", "exp_lmms", "FALSE", "exp_seconds", "0", "exp_dir", "", \
    "exp_python", "python3", "exp_scripts", EXPORT_SCRIPT_DIR

namespace exporter {

struct Job
{
    std::string input, out_base;
    int sub = 0;
};

// встроенный конвертер: пишет out_base.xm / out_base.mid, возвращает "" или текст ошибки
typedef std::string (*BuiltinFn)(const Job &, bool want_xm, bool want_midi);

struct Info
{
    const char * section;      // секция настроек
    const char * script;       // имя скрипта-конвертера
    bool style_b;              // true: -o X.xm --midi-file X.mid; false: -o X (база имени)
    const char * sub_opt;      // опция номера подтрека ("--track", "--song") или nullptr
    BuiltinFn builtin;         // встроенный конвертер (или nullptr)
    const char * const * exts; // расширения (без точки), nullptr в конце
};

inline bool ext_match(const std::string & path, const char * const * exts)
{
    size_t d = path.rfind('.');
    if (d == std::string::npos) return false;
    std::string e = path.substr(d + 1);
    for (auto & c : e) c = tolower(c);
    for (; *exts; exts++) if (e == *exts) return true;
    return false;
}

// запускает команду, возвращает код выхода; вывод (хвост) в out
inline int run(const std::vector<std::string> & args, std::string & out)
{
    int pfd[2];
    if (pipe(pfd)) return -1;
    pid_t pid = fork();
    if (pid < 0) return -1;
    if (pid == 0)
    {
        dup2(pfd[1], 1);
        dup2(pfd[1], 2);
        close(pfd[0]);
        close(pfd[1]);
        std::vector<char *> argv;
        for (auto & a : args) argv.push_back(const_cast<char *>(a.c_str()));
        argv.push_back(nullptr);
        execvp(argv[0], argv.data());
        _exit(127);
    }
    close(pfd[1]);
    char buf[4096];
    ssize_t n;
    while ((n = read(pfd[0], buf, sizeof buf)) > 0)
    {
        out.append(buf, n);
        if (out.size() > 8192) out.erase(0, out.size() - 4096);
    }
    close(pfd[0]);
    int st = 0;
    waitpid(pid, &st, 0);
    return WIFEXITED(st) ? WEXITSTATUS(st) : -1;
}

inline void notify(const std::string & title, const std::string & body)
{
    std::string o;
    run({"notify-send", "-a", "Audacious", title, body}, o);   // best effort
}

// вызывается из пункта меню (главный поток)
inline void export_selected(const Info & inf)
{
    bool want_xm = aud_get_bool(inf.section, "exp_xm");
    bool want_midi = aud_get_bool(inf.section, "exp_midi");
    const bool want_lmms = aud_get_bool(inf.section, "exp_lmms");
    if (!want_xm && !want_midi && !want_lmms)
    {
        aud_ui_show_error(_("Export: enable XM, MIDI and/or LMMS project in the plugin settings."));
        return;
    }
    // проект LMMS собирается из XM: если XM не нужен сам по себе, он пишется как промежуточный файл и удаляется
    const bool gen_xm = want_xm || want_lmms;
    std::string python = (const char *)aud_get_str(inf.section, "exp_python");
    std::string scripts = (const char *)aud_get_str(inf.section, "exp_scripts");
    std::string outdir = (const char *)aud_get_str(inf.section, "exp_dir");
    int secs = aud_get_int(inf.section, "exp_seconds");
    if (python.empty()) python = "python3";
    std::string script = scripts + "/" + inf.script;
    const std::string lmms_script = scripts + "/xm2mmpz.py";
    if (want_lmms && access(lmms_script.c_str(), R_OK))
    {
        aud_ui_show_error(str_printf(_("LMMS export needs %s (set the scripts folder in the plugin settings)."),
                                     lmms_script.c_str()));
        return;
    }
    const bool builtin_on = inf.builtin && aud_get_bool(inf.section, "exp_builtin");
    if (!builtin_on && access(script.c_str(), R_OK))
    {
        aud_ui_show_error(str_printf(_("Converter script not found: %s\nSet the scripts folder in the plugin settings."),
                                     script.c_str()));
        return;
    }

    std::vector<Job> jobs;
    auto pl = Playlist::active_playlist();
    for (int i = 0, n = pl.n_entries(); i < n; i++)
    {
        if (!pl.entry_selected(i)) continue;
        StringBuf p = uri_to_filename(pl.entry_filename(i));
        if (!p) continue;
        std::string f = (const char *)p;
        Job j;
        size_t q = f.rfind('?');
        if (q != std::string::npos) { j.sub = atoi(f.c_str() + q + 1); f.resize(q); }
        if (!ext_match(f, inf.exts)) continue;
        j.input = f;
        std::string dir, stem;
        size_t sl = f.rfind('/');
        dir = sl == std::string::npos ? "." : f.substr(0, sl);
        stem = f.substr(sl == std::string::npos ? 0 : sl + 1);
        size_t dot = stem.rfind('.');
        if (dot != std::string::npos) stem.resize(dot);
        if (j.sub > 0) stem += "_" + std::to_string(j.sub);
        if (!outdir.empty()) { mkdir(outdir.c_str(), 0755); dir = outdir; }
        j.out_base = dir + "/" + stem;
        jobs.push_back(j);
    }
    if (jobs.empty())
    {
        aud_ui_show_error(_("Export: select tracks of this plugin's format in the playlist."));
        return;
    }

    const bool use_builtin = inf.builtin && aud_get_bool(inf.section, "exp_builtin");
    std::thread([=]() {
        int ok = 0;
        std::string errors;
        // после XM: собрать проект LMMS (xm2mmpz.py), промежуточный XM удалить; "" или текст ошибки
        auto post = [&](const Job & j, bool xm_existed) -> std::string {
            if (!want_lmms) return "";
            std::string o;
            int rc = run({python, lmms_script, j.out_base + ".xm", "-o", j.out_base + ".mmpz"}, o);
            if (!want_xm && !xm_existed) unlink((j.out_base + ".xm").c_str());   // XM был только промежуточным
            if (rc == 0) return "";
            return "LMMS: " + (rc == 127 ? "cannot run " + python : o.substr(o.size() > 600 ? o.size() - 600 : 0));
        };
        for (const Job & j : jobs)
        {
            const bool xm_existed = access((j.out_base + ".xm").c_str(), F_OK) == 0;
            if (use_builtin)
            {
                std::string err = inf.builtin(j, gen_xm, want_midi);
                if (err.empty())
                {
                    err = post(j, xm_existed);
                    if (err.empty()) ok++;
                    else errors += j.input + ": " + err + "\n";
                    continue;
                }
                // "unsupported: ..." - встроенный конвертер не умеет этот файл, но скрипт может (USF ABI2/ABI3)
                if (err.rfind("unsupported:", 0) != 0 || access(script.c_str(), R_OK))
                {
                    errors += j.input + ": " + err + "\n";
                    continue;
                }
            }
            std::vector<std::string> a = {python, script, j.input};
            if (inf.style_b)
            {
                a.push_back("-o");
                a.push_back(j.out_base + ".xm");
                if (want_midi)
                {
                    a.push_back(gen_xm ? "--midi" : "--midi-only");
                    a.push_back("--midi-file");
                    a.push_back(j.out_base + ".mid");
                }
                else if (!gen_xm) continue;
            }
            else
            {
                a.push_back("-o");
                a.push_back(j.out_base);
                if (want_midi) a.push_back(gen_xm ? "--midi" : "--midi-only");
            }
            if (inf.sub_opt && j.sub > 0) { a.push_back(inf.sub_opt); a.push_back(std::to_string(j.sub)); }
            if (secs > 0) { a.push_back("--seconds"); a.push_back(std::to_string(secs)); }
            std::string out;
            int rc = run(a, out);
            if (rc == 0)
            {
                std::string err = post(j, xm_existed);
                if (err.empty()) ok++;
                else errors += j.input + ": " + err + "\n";
            }
            else errors += j.input + ": " + (rc == 127 ? "cannot run " + python : out.substr(out.size() > 600 ? out.size() - 600 : 0)) + "\n";
        }
        if (!errors.empty()) aud_ui_show_error(str_printf("Export errors:\n%s", errors.c_str()));
        if (ok) notify("Export finished", std::to_string(ok) + " file(s) converted");
    }).detach();
}

} // namespace exporter

// Определяет в плагине функции export_register()/export_unregister()
#define EXPORT_DEFINE(SEC, SCRIPT, STYLE_B, SUBOPT, LABEL, BUILTIN, ...) \
    static const char * const export_exts[] = {__VA_ARGS__, nullptr}; \
    static const exporter::Info export_info = {SEC, SCRIPT, STYLE_B, SUBOPT, BUILTIN, export_exts}; \
    static void export_cb() { exporter::export_selected(export_info); } \
    static void export_register() \
    { aud_plugin_menu_add(AudMenuID::Playlist, export_cb, LABEL, "document-save"); } \
    static void export_unregister() { aud_plugin_menu_remove(AudMenuID::Playlist, export_cb); }
