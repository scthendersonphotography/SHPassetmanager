# Pi TX ESP-NOW pairing recovery

The Laser Eye and Pi TX bridge each save one half of the link:

- the Laser Eye saves the bridge MAC as its Pi TX peer;
- the bridge saves the Laser Eye MAC as its accepted trigger sender.

The app shows **LINKED** only after both devices acknowledge the same pair
request. If either acknowledgement fails or times out, the app removes both
halves where they are reachable and does not retain a linked record.

## Recovery steps

1. Keep Bluetooth connected to the Laser Eye and connect WiFi to the Pi TX.
2. Confirm the bridge is powered and its MAC is visible in the app.
3. Select **Pair** again. Pairing overwrites the prior MAC on both devices, so
   it is the safe recovery action after an interrupted or uncertain pairing.
4. If unlinking while Pi TX WiFi is unavailable, the Laser Eye is still
   unlinked immediately (so it can no longer send triggers to the bridge).
   Reconnect to the Pi TX later and unlink once more, or pair again, to replace
   the bridge's retained sender record.

The Pi daemon returns `ERR_NO_BRIDGE` rather than a false success when its
UART bridge is disconnected. This lets the app keep the recovery state visible
instead of claiming that both devices were cleared.