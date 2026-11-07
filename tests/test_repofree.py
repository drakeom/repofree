"""Tests for repofree. Run:  python3 -m unittest discover -s tests -v   (no installs needed)"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import repofree as ra  # noqa: E402

FIXTURE = {
    "server/app.js": """
        const express = require('express');
        const { helper } = require('./lib/helper');
        const app = express();

        // app.post('/commented-out', handler)  <- must be ignored
        app.post('/orders/create', verifyToken, async (req, res) => {
          const total = computeTotal(req.body.items);
          await db.collection('orders').doc(req.body.id).set({ total });
          helper();
          res.json({ ok: true });
        });

        app.get('/orders/:id', async (req, res) => {
          const snap = await db.collection('orders').doc(req.params.id).get();
          res.json(snap.data());
        });

        async function reconcile() {
          const all = await db.collectionGroup('refunds').where('status', '==', 'pending').get();
          const few = await db.collection('payments').limit(50).get();
          return all.size + few.size;
        }

        function computeTotal(items) {
          return items.reduce((s, i) => s + price(i), 0);
        }
        const price = (i) => i.qty * i.unit;
        function verifyToken(req, res, next) { next(); }

        exports.Orders = onRequest(app);
        exports.reconcileJob = onSchedule('every 5 minutes', async () => { await reconcile(); });
        const key = process.env.PAYMENT_KEY;
    """,
    "server/lib/helper.js": """
        function helper() { return format('x'); }
        function format(s) { return `[${s}]`; }
        module.exports = { helper };
    """,
    "public/js/orders-page.js": """
        class OrdersPage {
          constructor() {
            document.addEventListener('storeLoaded', () => this.load());
          }
          async load() {
            const base = 'https://example.com/Orders';
            const r = await fetch(`${base}/orders/create`, { method: 'POST' });
            localStorage.setItem('lastOrder', 'x');
            this.render();
          }
          render() {
            const html = `<button onclick="openOrder('1')">Open</button>`;
            return html;
          }
        }
        function openOrder(id) { return id; }
        window.openOrder = openOrder;
        const notACall = "fetch('/not/real')";
        const re = /fetch\\(x\\)/g;
    """,
    "public/js/auth.js": """
        function signIn() {
          window.dispatchEvent(new CustomEvent('storeLoaded'));
          sessionStorage.getItem('storeId');
        }
    """,
    "public/orders.html": """
        <!doctype html>
        <html><head>
          <link rel="stylesheet" href="css/app.css?v=3">
        </head><body>
          <button onclick="signIn()">Sign in</button>
          <!-- <script src="js/old.js"></script> -->
          <script src="js/auth.js?v=12"></script>
          <script src="js/orders-page.js?v=7"></script>
          <script>
            function boot() { new OrdersPage(); }
            boot();
          </script>
        </body></html>
    """,
    "public/other.html": """
        <html><head><link rel="stylesheet" href="css/app.css?v=2"></head>
        <body><script src="js/auth.js"></script></body></html>
    """,
    "public/css/app.css": "body { color: black; }\n",
    "tools/report.py": """
        import os
        from fastapi import FastAPI
        app = FastAPI()

        class Reporter:
            def build(self):
                return summarize(load())

        def load():
            return os.getenv("REPORT_DB")

        def summarize(rows):
            return len(rows)

        @app.get("/reports/{rid}")
        def get_report(rid):
            return Reporter().build()
    """,
    "svc/main.go": """
        package main

        import (
            "fmt"
            "net/http"
        )

        type Store struct{}

        func (s *Store) Save(x int) error {
            fmt.Println(x)
            return nil
        }

        func main() {
            http.HandleFunc("/health", health)
            s := &Store{}
            s.Save(1)
        }

        func health(w http.ResponseWriter, r *http.Request) {}
    """,
    "app/lib/main.dart": """
        import 'package:flutter/material.dart';

        class HomePage extends StatelessWidget {
          Widget build(BuildContext context) {
            return Text(greet('x'));
          }
        }

        String greet(String n) => 'hi $n';
    """,
}


def write_fixture(root):
    for rel, body in FIXTURE.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write(textwrap.dedent(body).lstrip("\n"))


def run(root, *args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = ra.main(["--repo", root, "--json", *args])
    assert code == 0, err.getvalue()
    return json.loads(out.getvalue())


class Base(unittest.TestCase):
    use_git = False

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="repofree-test-")
        write_fixture(self.root)
        if self.use_git:
            subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
            subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
            subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
                           cwd=self.root, check=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)


class TestJavaScript(Base):
    def test_definitions(self):
        names = {r["qual"] for r in run(self.root, "find", "o")["results"]}
        self.assertIn("OrdersPage.load", names)
        self.assertIn("openOrder", names)
        found = run(self.root, "find", "price")["results"]
        self.assertEqual(found[0]["file"], "server/app.js")

    def test_callers_inside_route_handlers_are_named(self):
        res = run(self.root, "callers", "computeTotal")["callers"]
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["in"], "POST /orders/create")

    def test_middleware_and_html_handlers_count_as_callers(self):
        vias = {r["via"] for r in run(self.root, "callers", "verifyToken")["callers"]}
        self.assertIn("callback/ref", vias)
        html = run(self.root, "callers", "openOrder")["callers"]
        self.assertTrue(any(r["via"] == "html handler" for r in html), html)
        page = run(self.root, "callers", "signIn")["callers"]
        self.assertTrue(any(r["file"] == "public/orders.html" and r["via"] == "html handler" for r in page))

    def test_comments_and_strings_are_not_code(self):
        routes = [r["path"] for r in run(self.root, "endpoints")["routes"]]
        self.assertNotIn("/commented-out", routes)
        um = run(self.root, "endpoints", "--unmatched")["unmatched"]
        self.assertFalse(any("/not/real" in u["url"] for u in um), um)

    def test_callees(self):
        res = run(self.root, "callees", "computeTotal")["results"][0]["calls"]
        self.assertEqual([r["name"] for r in res], ["price"])

    def test_path(self):
        p = run(self.root, "path", "reconcileJob", "reconcile")
        self.assertIsNotNone(p["path"])

    def test_cross_file_import(self):
        f = run(self.root, "file", "server/lib/helper.js")
        self.assertTrue(any(x["file"] == "server/app.js" for x in f["loaded_by"]))


class TestFacts(Base):
    def test_endpoint_links_frontend_to_route_and_service(self):
        routes = {r["path"]: r for r in run(self.root, "endpoints")["routes"]}
        create = routes["/orders/create"]
        self.assertEqual(create["service"], ["Orders"])
        self.assertTrue(any(c["file"] == "public/js/orders-page.js" for c in create["clients"]))
        self.assertIn("/reports/{rid}", routes)
        self.assertIn("/health", routes)

    def test_collections_read_write_and_limits(self):
        sites = run(self.root, "collections", "orders")["sites"]
        modes = sorted(s["mode"] for s in sites)
        self.assertEqual(modes, ["read", "write"])
        refunds = run(self.root, "collections", "refunds")["sites"][0]
        self.assertIn("no-limit-seen", refunds["flags"])
        self.assertEqual(refunds["api"], "collectionGroup")
        payments = run(self.root, "collections", "payments")["sites"][0]
        self.assertNotIn("no-limit-seen", payments["flags"])

    def test_events(self):
        ev = {e["event"]: e for e in run(self.root, "events")["events"]}
        self.assertIn("storeLoaded", ev)
        self.assertEqual(ev["storeLoaded"]["emitted_by"][0]["file"], "public/js/auth.js")
        self.assertEqual(ev["storeLoaded"]["listened_by"][0]["file"], "public/js/orders-page.js")

    def test_storage_env_globals_services(self):
        keys = {r["key"] for r in run(self.root, "storage")["storage"]}
        self.assertEqual(keys, {"lastOrder", "storeId"})
        env = {r["name"] for r in run(self.root, "env")["env"]}
        self.assertEqual(env, {"PAYMENT_KEY", "REPORT_DB"})
        self.assertEqual([g["name"] for g in run(self.root, "globals")["global"]], ["openOrder"])
        fns = {f["name"]: f["trigger"] for f in run(self.root, "services")["functions"]}
        self.assertEqual(fns, {"Orders": "onRequest", "reconcileJob": "onSchedule"})

    def test_versions(self):
        files = {f["file"]: f for f in run(self.root, "versions", "--all")["files"]}
        self.assertEqual(files["public/css/app.css"]["issue"], "different versions")
        self.assertEqual(files["public/js/auth.js"]["issue"], "some pages have no version")
        self.assertIsNone(files["public/js/orders-page.js"]["issue"])
        self.assertNotIn("public/js/old.js", files)  # commented-out script tag

    def test_impact_reaches_pages(self):
        imp = run(self.root, "impact", "openOrder")
        self.assertIn("public/orders.html", imp["pages"])

    def test_compare(self):
        c = run(self.root, "compare", "server", "tools")
        only_a = {x["name"] for x in c["sections"]["functions"]["only_in_a"]}
        self.assertIn("computeTotal", only_a)


class TestOtherLanguages(Base):
    def test_python(self):
        self.assertEqual(run(self.root, "find", "Reporter.build")["results"][0]["kind"], "method")
        callers = run(self.root, "callers", "summarize")["callers"]
        self.assertEqual(callers[0]["in"], "Reporter.build")

    def test_go_and_dart(self):
        self.assertEqual(run(self.root, "find", "Save")["results"][0]["qual"], "Store.Save")
        self.assertEqual(run(self.root, "callers", "health")["callers"][0]["file"], "svc/main.go")
        self.assertEqual(run(self.root, "callers", "greet")["callers"][0]["in"], "HomePage.build")


class TestRefresh(Base):
    use_git = True

    def test_auto_refresh_add_change_delete(self):
        self.assertEqual(run(self.root, "find", "brandNew")["results"], [])
        p = os.path.join(self.root, "public/js/new.js")
        with open(p, "w") as fh:
            fh.write("function brandNew() { return 1; }\n")
        self.assertEqual(run(self.root, "find", "brandNew")["results"][0]["file"], "public/js/new.js")
        time.sleep(0.01)
        with open(p, "w") as fh:
            fh.write("\n\nfunction brandNew() { return 2; }\n")
        self.assertEqual(run(self.root, "find", "brandNew")["results"][0]["line"], 3)
        os.remove(p)
        self.assertEqual(run(self.root, "find", "brandNew")["results"], [])

    def test_map_folder_ignores_itself_in_git(self):
        run(self.root, "status")
        st = subprocess.run(["git", "status", "--porcelain"], cwd=self.root, capture_output=True, text=True).stdout
        self.assertEqual(st.strip(), "")

    def test_custom_dir_and_options_after_command(self):
        d = tempfile.mkdtemp(prefix="repofree-dir-")
        try:
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                ra.main(["status", "--repo", self.root, "--dir", d, "--json"])
            self.assertTrue(os.path.isfile(os.path.join(d, "map.json")))
            self.assertFalse(os.path.exists(os.path.join(self.root, ".repofree")))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_versions_changed_detects_missed_bump(self):
        with open(os.path.join(self.root, "public/js/auth.js"), "a") as fh:
            fh.write("// edited\n")
        rows = run(self.root, "versions", "--changed")["changed"]
        self.assertTrue(rows and all(not r["bumped"] for r in rows), rows)
        page = os.path.join(self.root, "public/orders.html")
        with open(page) as fh:
            html = fh.read()
        with open(page, "w") as fh:
            fh.write(html.replace("js/auth.js?v=12", "js/auth.js?v=13"))
        rows = {r["page"]: r for r in run(self.root, "versions", "--changed")["changed"]}
        self.assertTrue(rows["public/orders.html"]["bumped"])
        self.assertFalse(rows["public/other.html"]["bumped"])

    def test_export_and_report(self):
        res = run(self.root, "export")
        self.assertTrue(os.path.isfile(res["export"]))
        with open(os.path.join(self.root, ".repofree", "REPOFREE.md")) as fh:
            self.assertIn("## HTTP routes", fh.read())


DOCS_FIXTURE = {
    "web/util.js": """
        /**
         * Formats a price in rupees.
         * @param {number} amount - value in paise
         * @returns {string} display text
         */
        function formatPrice(amount) { return String(amount / 100); }

        // Sends the order to the server.
        // Retries once on failure.

        async function submitOrder(order) {
          const log = (m) => console.log(m);
          log('start');
          return formatPrice(order.total);
        }

        // ---- helpers ----
        function noDocHelper() { return [1].forEach(x => x); }

        // const old = submitOrder(x);
        function afterCommentedCode() { return 1; }

        function other() {
          const log = (m) => m;
          log('other');
        }
    """,
    "web/page.html": """
        <html><body>
        <script src="util.js"></script>
        <script>
          document.getElementById('b').addEventListener('click', () => submitOrder({ total: 5 }));
          noDocHelper();
        </script>
        </body></html>
    """,
    "svc/tool.py": """
        def compute(a, b=2):
            \"\"\"Add two numbers.

            Used by the report.\"\"\"
            return a + b


        def bare():
            return compute(1)
    """,
    "tests/stub.js": """
        function formatPrice() { return 'stub'; }
        function getElementById() {}
    """,
}


class TestDocs(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="repofree-docs-")
        for rel, body in DOCS_FIXTURE.items():
            path = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(textwrap.dedent(body).lstrip("\n"))
        self.res = run(self.root, "docs")
        with open(os.path.join(self.res["docs"], "data.js")) as fh:
            t = fh.read()
        self.D = json.loads(t[len("window.REPOFREE_DOCS="):-2])

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def fn(self, qual, path=None):
        for fn in self.D["fns"]:
            if fn["q"] == qual and (path is None or self.D["files"][fn["f"]]["p"] == path):
                return fn
        self.fail("no function %s" % qual)

    def test_files_written(self):
        for name in ("index.html", "graph.html", "data.js", "missing-comments.md"):
            self.assertTrue(os.path.isfile(os.path.join(self.res["docs"], name)), name)
        with open(os.path.join(self.res["docs"], "graph.html")) as fh:
            self.assertIn("var START = 'graph'", fh.read())

    def test_jsdoc(self):
        d = self.fn("formatPrice", "web/util.js")["doc"]
        self.assertEqual(d["text"], "Formats a price in rupees.")
        self.assertEqual(d["params"][0][:2], ["amount", "number"])
        self.assertIn("display text", d["returns"])
        self.assertEqual(self.fn("formatPrice", "web/util.js")["sig"], "formatPrice(amount)")

    def test_line_comments_with_blank_line(self):
        d = self.fn("submitOrder")["doc"]
        self.assertIn("Sends the order to the server.", d["text"])
        self.assertIn("Retries once", d["text"])

    def test_section_headers_and_commented_code_are_not_docs(self):
        self.assertNotIn("doc", self.fn("noDocHelper"))
        self.assertNotIn("doc", self.fn("afterCommentedCode"))

    def test_python_docstring(self):
        fn = self.fn("compute")
        self.assertTrue(fn["doc"]["text"].startswith("Add two numbers."))
        self.assertEqual(fn["doc"]["src"], "docstring")
        self.assertEqual(fn["sig"], "compute(a, b=2)")

    def test_callers_and_scoping(self):
        files = self.D["files"]
        sub = self.fn("submitOrder")
        self.assertEqual(len(sub["callers"]), 1)
        self.assertEqual(files[sub["callers"][0][1]]["p"], "web/page.html")
        # formatPrice is called from util.js, never from the test stub's copy
        self.assertEqual(len(self.fn("formatPrice", "web/util.js")["callers"]), 1)
        self.assertNotIn("callers", self.fn("formatPrice", "tests/stub.js"))
        self.assertNotIn("callers", self.fn("getElementById"))
        # each nested `log` only gets the call inside its own function
        logs = [fn for fn in self.D["fns"] if fn["n"] == "log"]
        self.assertEqual(sorted(len(fn.get("callers", [])) for fn in logs), [1, 1])

    def test_missing_list(self):
        names = [r["name"] for r in self.res["missing"]]
        self.assertIn("noDocHelper", names)
        self.assertIn("bare", names)
        self.assertNotIn("formatPrice", names[:1])
        self.assertNotIn("submitOrder", names)
        stub_rows = [r for r in self.res["missing"] if r["test"]]
        self.assertTrue(stub_rows and names.index(stub_rows[0]["name"]) >= len(names) - len(stub_rows))
        with open(os.path.join(self.res["docs"], "missing-comments.md")) as fh:
            md = fh.read()
        self.assertIn("noDocHelper", md)
        only = run(self.root, "docs", "--missing", "svc/")
        self.assertEqual([r["name"] for r in only["missing"]], ["bare"])

class TestLexer(unittest.TestCase):
    def test_template_expressions_stay_code(self):
        src = "const a = `x ${f(1)} y`; // g(2)\nconst r = /h\\(3\\)/;"
        c, s = ra.lex(src, js=True, backtick="template")
        ns = ra._blank(src, c + s)
        self.assertIn("f(1)", ns)
        self.assertNotIn("g(2)", ns)
        self.assertNotIn("h", ns.split("=")[-1])

    def test_division_is_not_regex(self):
        src = "const x = a / b; const y = call(1) / 2; fn(3);"
        c, s = ra.lex(src, js=True, backtick="template")
        self.assertIn("fn(3)", ra._blank(src, c + s))


if __name__ == "__main__":
    unittest.main()
