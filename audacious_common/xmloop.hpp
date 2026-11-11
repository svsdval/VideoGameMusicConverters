// Бесшовные петли XM: порт xm_export.smooth_loop (Python), результат побайтно тот же.
// Если скачок с конца петли на её начало заметно хуже самого резкого места внутри петли
// (из-за этого длинная нота «трещит» на стыке), подбирается близкая пара начало/конец, у которой
// 32 кадра перед концом лучше всего совпадают с 32 кадрами перед началом. Данные не меняются,
// двигаются только точки петли (конец - только к началу).
#pragma once
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <queue>
#include <utility>
#include <vector>

namespace xmloop {

constexpr int LOOP_MIN = 64;   // более короткие петли (однопериодные волны чипов) не трогаем
constexpr int LOOP_CTX = 32;   // окно сравнения «перед началом» и «перед концом»

inline bool enabled()
{
    const char * e = std::getenv("XM_LOOP_FIX");     // как в Python: XM_LOOP_FIX=0 отключает
    return !(e && e[0] == '0' && e[1] == 0);
}

// Ошибка стыка: предсказание по уровню и наклону: x[ls] против 2*x[e-1] - x[e-2].
inline double wrap_error(const std::vector<int16_t> & x, int ls, int e)
{
    return std::fabs((double)x[ls] - (2.0 * x[e - 1] - x[e - 2]));
}

// -> true, если петля изменена (nls, nll - новые начало и длина)
inline bool smooth_loop(const std::vector<int16_t> & x, int ls, int ll, int & nls, int & nll)
{
    const int ctx = LOOP_CTX;
    nls = ls; nll = ll;
    if (ll < LOOP_MIN || ls < 0 || (size_t)ls + ll > x.size()) return false;
    const int e0 = ls + ll;
    double d2max = 0;
    for (int i = ls + 1; i + 1 < e0; i++)
        d2max = std::max(d2max, std::fabs((double)x[i + 1] - 2.0 * x[i] + x[i - 1]));
    const double c0 = wrap_error(x, ls, e0);
    if (c0 <= 1.05 * d2max + 2.0) return false;
    const int ws = std::max(std::min(ll / 8, 1024), ctx - ls);
    const int we = std::min(ll / 4, 4096);
    const int ls_first = std::max(ls, ctx), ls_last = ls + ws;
    if (ls_first > ls_last) return false;
    const int e_first = std::max(ls_first + ll / 2, e0 - we), e_last = e0;
    if (e_first > e_last) return false;
    const int nB = ls_last - ls_first + 1, nA = e_last - e_first + 1;
    // D[i][j] = |A_i|^2 + |B_j|^2 - 2 A_i.B_j  (целые числа, точны и в double)
    std::vector<double> sa(nA), sb(nB);
    for (int i = 0; i < nA; i++)
    {
        long long t = 0;
        for (int k = 0; k < ctx; k++) { long long v = x[e_first + i - ctx + k]; t += v * v; }
        sa[i] = (double)t;
    }
    for (int j = 0; j < nB; j++)
    {
        long long t = 0;
        for (int k = 0; k < ctx; k++) { long long v = x[ls_first + j - ctx + k]; t += v * v; }
        sb[j] = (double)t;
    }
    // 64 лучших пар: наименьшее D, при равенстве - меньший номер (как устойчивая сортировка numpy)
    typedef std::pair<double, long long> DI;
    std::priority_queue<DI> heap;                       // максимум наверху
    for (int i = 0; i < nA; i++)
        for (int j = 0; j < nB; j++)
        {
            long long dot = 0;
            for (int k = 0; k < ctx; k++) dot += (long long)x[e_first + i - ctx + k] * x[ls_first + j - ctx + k];
            DI cand((double)((long long)sa[i] + (long long)sb[j] - 2 * dot), (long long)i * nB + j);
            if (heap.size() < 64) heap.push(cand);
            else if (cand < heap.top()) { heap.pop(); heap.push(cand); }
        }
    std::vector<DI> order;
    while (!heap.empty()) { order.push_back(heap.top()); heap.pop(); }
    std::sort(order.begin(), order.end());
    bool have = false;
    double best_err = 0;
    int best_ls = ls, best_e = e0;
    for (const DI & d : order)
    {
        int i = (int)(d.second / nB), j = (int)(d.second % nB);
        int cls = ls_first + j, ce = e_first + i;
        double err = wrap_error(x, cls, ce);
        if (!have || err < best_err) { have = true; best_err = err; best_ls = cls; best_e = ce; }
    }
    if (!have || best_err > 0.5 * c0) return false;
    nls = best_ls; nll = best_e - best_ls;
    return true;
}

// Применить к сэмплу: при необходимости обрезает data и обновляет (ls, ll). loop_on - есть ли прямая петля.
inline bool fix_loop(std::vector<int16_t> & data, int & ls, int & ll, bool loop_on)
{
    if (!loop_on || !enabled()) return false;
    int nls, nll;
    if (!smooth_loop(data, ls, ll, nls, nll)) return false;
    data.resize((size_t)nls + nll);
    ls = nls; ll = nll;
    return true;
}

} // namespace xmloop
