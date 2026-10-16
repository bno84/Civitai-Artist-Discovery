# Third-party notices

The Windows build includes these unmodified Python libraries:

- **pystray 0.19.5** — GNU Lesser General Public License v3.0; source and project details:
  <https://pypi.org/project/pystray/0.19.5/>
- **Pillow 12.3.0** — HPND License; source and project details:
  <https://pypi.org/project/pillow/12.3.0/>

The release folder includes each library's complete license text under `licenses/`.
These libraries are used only for the Windows notification-area icon. They are not
installed or imported by Linux runtime builds.

The portable executable also includes the unmodified Python runtime and PyInstaller
bootloader. Their supplied license texts are included as `licenses/Python-LICENSE.txt`
and `licenses/PyInstaller-COPYING.txt`. The application's MIT license is `LICENSE.txt`.
See [Python's license](https://docs.python.org/3.14/license.html) and
[PyInstaller's licensing terms](https://pyinstaller.org/en/stable/license.html).
