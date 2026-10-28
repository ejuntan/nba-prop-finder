// The page prices lines in the browser. If its negative binomial disagrees
// with scipy's, every number on it is wrong, so lift the maths straight out
// of index.html and make it match.
var here = "/Users/tane-jun/nba_props/tool/";
var page = readFile(here + "index.html");
var truth = JSON.parse(readFile(here + "truth.json"));
var a = page.indexOf("function lgamma");
var b = page.indexOf("/* ---------- helpers");
if (a < 0 || b < 0) { print("FAIL: could not find the maths block"); }
eval(page.slice(a, b));

var fails = 0, n = 0, worst = 0, worstWhat = "";
function near(got, want, tol, what) {
  n++;
  var d = Math.abs(got - want);
  if (d > worst) { worst = d; worstWhat = what; }
  if (!(d <= tol)) {
    fails++;
    if (fails <= 8) print("  FAIL " + what + ": js " + got + " vs py " + want);
  }
}
truth.cases.forEach(function (c) {
  var cc = truth.calib[c.stat];
  var tag = c.stat + " mu=" + c.mu + " line=" + c.line;
  near(pOver(c.mu, cc, c.line), c.p_over, 2e-6, "p_over " + tag);
  near(pPush(c.mu, cc, c.line), c.p_push, 2e-6, "p_push " + tag);
  near(sdOf(c.mu, cc), c.sd, 1e-9, "sd " + tag);
  near(fairLine(c.mu, cc), c.fair, 1e-9, "fair line " + tag);
  near(quant(c.mu, cc, 0.5), c.q50, 1e-9, "median " + tag);
});
truth.odds.forEach(function (o) {
  var d = devig(o.o, o.u);
  near(d[0], o.po, 1e-9, "devig over " + o.o + "/" + o.u);
  near(d[1], o.pu, 1e-9, "devig under " + o.o + "/" + o.u);
  near(a2p(o.o), o.a2p, 1e-12, "a2p " + o.o);
  near(p2a(0.5349), o.p2a, 1e-12, "p2a");
  near(ev(0.55, o.o), o.ev_o, 1e-12, "ev " + o.o);
  near(kelly(0.55, o.o), o.kelly_o, 1e-12, "kelly " + o.o);
});
print((fails ? "mathtest FAILED " + fails + "/" + n
             : "mathtest ok " + n + " checks") +
      "  (largest gap " + worst.toExponential(2) + " on " + worstWhat + ")");
