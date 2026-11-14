# Third-party notices

The direct-camera worker in `insta_ble_direct_worker.py` is an original BlueZ
D-Bus implementation.  Its BE80/BE81/BE82 UUIDs and phone-command packet facts
were validated against these public protocol references:

- Hackaday.io, “Insta360 X3 BLE remote control with ESP32”:
  <https://hackaday.io/project/188975-insta360-x3-ble-remote-control-with-esp32/details>
- Whitebox Insta360 protocol documentation and GPL-3.0 project:
  <https://insta360.whitebox.aero/developer-guide/packet-anatomy/>
  and <https://gitlab.com/whitebox-aero/insta360>
- Whitebox `lib-one-proto` protobuf definitions, GPL-3.0-or-later:
  <https://gitlab.com/whitebox-aero/insta360-lib-one-proto>

No source code from the GPL projects is copied into the direct worker; the
references are listed to document protocol provenance and interoperability.

The GATT service layout, characteristic UUIDs, static values, remote name and
shutter payload in `insta_ble_worker.py` are derived from:

- `pchwalek/insta360_ble_esp32`, Copyright (c) 2026 pchwalek, MIT License.
  <https://github.com/pchwalek/insta360_ble_esp32>

The upstream MIT license text follows.

~~~text
MIT License

Copyright (c) 2026 pchwalek

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
~~~
