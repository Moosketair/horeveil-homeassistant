package fr.moosketair.horeveil.platform.homeassistant

import com.google.zxing.BinaryBitmap
import com.google.zxing.RGBLuminanceSource
import com.google.zxing.common.HybridBinarizer
import com.google.zxing.qrcode.QRCodeReader
import javax.imageio.ImageIO

fun main() {
    val image = checkNotNull(ImageIO.read(System.`in`))
    val width = image.width
    val height = image.height
    val pixels = image.getRGB(0, 0, width, height, null, 0, width)
    val decoded = QRCodeReader().decode(BinaryBitmap(HybridBinarizer(RGBLuminanceSource(width, height, pixels))))
    val qr = checkNotNull(PairingInvitation.qr(decoded.text))
    val manual = checkNotNull(PairingInvitation.manual(qr.origin, qr.code))
    check(qr.origin == manual.origin && qr.code == manual.code)
    checkNotNull(qr.expiresAt)
}
