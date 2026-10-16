"""Собирает tilda_block.html из static/index.html. Запуск: python build_tilda.py
После любой правки интерфейса в static/index.html запустите заново и вставьте результат в Tilda."""
import re

SERVER = "https://neyroyaga-cakes-production.up.railway.app"   # адрес вашего сервера на Railway
P = ".vb-x7k"

src = open("static/index.html", encoding="utf-8").read()
css = src[src.index("<style>") + 7 : src.index("</style>")]
body = src[src.index("<body>") + 6 : src.index("<script>")]
js = src[src.index("<script>") + 8 : src.rindex("</script>")]


def scope(css):
    out, i = [], 0
    while True:
        j = css.find("{", i)
        if j < 0:
            return "\n".join(out)
        sel, depth, k = css[i:j].strip(), 1, j + 1
        while depth:
            depth += (css[k] == "{") - (css[k] == "}")
            k += 1
        inner = css[j + 1 : k - 1]
        if sel.startswith("@media"):
            out.append(f"{sel}{{{scope(inner)}}}")
        else:
            parts = []
            for s in (x.strip() for x in sel.split(",")):
                s = re.sub(r"#([A-Za-z][\w-]*)", r"#ny-\1", s)          # id в селекторах получают префикс
                parts.append(P if s in (":root", "body", "html") else f"{P} *" if s == "*" else f"{P} {s}")
            out.append(f"{','.join(parts)}{{{inner}}}")
        i = k


css = scope(css) + f"\n{P}{{border-radius:18px;padding-bottom:24px}}{P} .login{{min-height:600px}}{P} header{{position:static}}"

# разметка: уникальные id и без inline-обработчиков (в Tilda глобальный $ — это jQuery)
body = body.replace("""onclick="$('dlg').close()\"""", 'id="dclose"')
body = re.sub(r'id="([A-Za-z0-9_-]+)"', r'id="ny-\1"', body)

# скрипт: адрес сервера, префикс id, изоляция в функции
js = re.sub(r"// Если страница лежит.*\n", "", js)
js = js.replace("const ORIGIN=window.API_BASE||location.origin,", f"const ORIGIN='{SERVER}',")
js = re.sub(r"\$\('([A-Za-z0-9_-]+)'\)", r"$('ny-\1')", js)
js = js.replace("labels();\n</script>", "labels();").rstrip()
assert js.endswith("labels();")
js = "(function(){\n" + js + "\n$('ny-dclose').onclick=()=>$('ny-dlg').close();\n})();"

# id должны передаваться строкой: $(переменная) сборщик переименовать не может
dynamic = re.findall(r"\$\((?!')[^)]{0,40}\)", js)
assert not dynamic, f"в скрипте есть $() с переменной, сборка сломается: {dynamic[:3]}"

# поиск по классам опасен: у блоков Tilda бывают такие же классы, и скрипт цепляется к чужому элементу
globals_ = re.findall(r"document\.querySelector(?:All)?\([^)]*\)", js)
assert not globals_, f"ищите элементы по id через $('...'), а не по классу: {globals_[:3]}"

html_ids = set(re.findall(r'id="(ny-[^"]+)"', body))
missing = set(re.findall(r"\$\('(ny-[^']+)'\)", js)) - html_ids
assert not missing, f"в скрипте есть id без элемента: {missing}"

out = f"""<!-- НейроЯга: блок для Tilda. Вставьте целиком в блок T123 «HTML-код».
     Адрес сервера задан в скрипте: const ORIGIN. Файл создан скриптом build_tilda.py -->
<section class="vb-x7k" id="neyroyaga-studio" data-section-name="НейроЯга — макеты для печати" data-vibeblock-unique-section="yes">
<style>
{css}
</style>
{body.strip()}
<script>
{js}
</script>
</section>
"""
open("tilda_block.html", "w", encoding="utf-8").write(out)
print("tilda_block.html:", len(out), "байт")
