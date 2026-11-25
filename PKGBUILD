# Maintainer: you
pkgname=wowspace
pkgver=1.1.0
pkgrel=1
pkgdesc="WOW Thing-style sound enhancer (3D space, TruBass, focus, crossfeed) for PipeWire"
arch=('any')
license=('MIT')
depends=('python' 'pyside6' 'pipewire' 'wireplumber')
source=('wowspace.py' 'wowspace.desktop' 'wowspace.svg' 'LICENSE')
sha256sums=('SKIP' 'SKIP' 'SKIP' 'SKIP')

package() {
  install -Dm755 "$srcdir/wowspace.py" "$pkgdir/usr/bin/wowspace"
  install -Dm644 "$srcdir/wowspace.desktop" "$pkgdir/usr/share/applications/wowspace.desktop"
  install -Dm644 "$srcdir/wowspace.svg" "$pkgdir/usr/share/icons/hicolor/scalable/apps/wowspace.svg"
  install -Dm644 "$srcdir/LICENSE" "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}
