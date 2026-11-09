// t_kext_od.cpp - MI50BarFix.kext's overdrive-file frame check (od_file_ok), compiled from the kext's own source: build_and_test.sh cuts the
// lines between MI50BarFix.cpp's "BEGIN od_file_ok" and "END od_file_ok" markers into od_file_ok.inc. The kext publishes the file as the
// card's "MI50 Overdrive" property only when od_file_ok returns NULL; the CRC and the values are the TinyGPU driver's to check (t_dext F).
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>
#include "od_file_ok.inc"

static int nfail = 0, nok = 0;
static void check(bool c, const std::string &m) { printf("%s %s\n", c ? "ok  " : "FAIL", m.c_str()); if (c) nok++; else nfail++; }
static std::string why(const std::vector<unsigned char> &b) { const char *w = od_file_ok(b.data(), b.size()); return w ? w : "NULL"; }

int main() {
  std::vector<unsigned char> f(44, 0xA5);
  memcpy(f.data(), "MI50OD01", 8);
  check(why(f) == "NULL", "K1: 44 bytes starting with MI50OD01 -> published (the rest, CRC included, is the driver's to check)");
  int bad = 0;
  for (size_t n : {0, 1, 8, 43, 45, 46, 64}) { std::vector<unsigned char> g(f); g.resize(n, 0); if (why(g) != "not 44 bytes") bad++; }
  check(bad == 0, "K2: 0, 1, 8, 43, 45, 46 (the old frame) and 64 bytes -> not 44 bytes");
  bad = 0;
  for (int i = 0; i < 8; i++) { std::vector<unsigned char> g(f); g[i] ^= 0x20; if (why(g) != "no MI50OD01 magic") bad++; }
  check(bad == 0, "K3: any one magic byte changed (here: its case) -> no MI50OD01 magic");
  printf("\n%d ok, %d FAIL\n", nok, nfail);
  return nfail ? 1 : 0;
}
