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
  // The page prices in decimal. The conversion has to be exact, and the
  // decimal-native EV, Kelly and devig have to agree with the American
  // ones they replaced -- otherwise the display changed the numbers.
  near(a2d(o.o), o.dec_o, 1e-12, "a2d " + o.o);
  near(a2d(o.u), o.dec_u, 1e-12, "a2d " + o.u);
  near(p2d(0.5349), o.p2d, 1e-12, "p2d");
  near(evD(0.55, a2d(o.o)), o.ev_o, 1e-12, "evD " + o.o);
  near(kellyD(0.55, a2d(o.o)), o.kelly_o, 1e-12, "kellyD " + o.o);
  var dd = devigD(a2d(o.o), a2d(o.u));
  near(dd[0], o.po, 1e-12, "devigD over " + o.o + "/" + o.u);
  near(dd[1], o.pu, 1e-12, "devigD under " + o.o + "/" + o.u);
});
// The normal, and the copula that combines correlated legs. The joint
// probability is Monte Carlo, so it is checked at a draw count where the
// sampling error is an order of magnitude under the tolerance -- 200k
// draws put the standard error near 1e-3.
truth.normal.cdf.forEach(function (c) {
  near(ncdf(c.z), c.p, 1e-12, "ncdf " + c.z);
});
truth.normal.ppf.forEach(function (c) {
  near(nppf(c.p), c.z, 1e-9, "nppf " + c.p);
});
var worstJoint = 0;
truth.joint.forEach(function (c) {
  var C = [[1, c.rho], [c.rho, 1]];
  var got = jointProb([c.pa, c.pb], C, 200000);
  worstJoint = Math.max(worstJoint, Math.abs(got - c.p));
  near(got, c.p, 5e-3,
       "joint " + c.pa + "x" + c.pb + " rho=" + c.rho);
});
// Independence has a closed form, and the copula must reproduce it.
near(jointProb([0.6, 0.5, 0.4], [[1, 0, 0], [0, 1, 0], [0, 0, 1]], 200000),
     0.6 * 0.5 * 0.4, 5e-3, "three independent legs");
// Perfect correlation means the legs are one bet: the joint is the
// smallest of them, not their product.
near(jointProb([0.6, 0.5], [[1, 1], [1, 1]], 50000), 0.5, 5e-3,
     "two identical legs");
print("  parlay: worst gap against scipy " + worstJoint.toExponential(2));

print((fails ? "mathtest FAILED " + fails + "/" + n
             : "mathtest ok " + n + " checks") +
      "  (largest gap " + worst.toExponential(2) + " on " + worstWhat + ")");
