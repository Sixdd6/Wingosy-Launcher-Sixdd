package com.nendo.argosy.ui.util

import androidx.compose.animation.animateColorAsState
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.composed
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Paint
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.graphics.drawscope.drawIntoCanvas
import androidx.compose.ui.graphics.nativeCanvas
import androidx.compose.ui.unit.Dp
import com.nendo.argosy.ui.theme.Motion

fun Modifier.focusGlow(
    color: Color,
    blurRadius: Float = 16f,
    spread: Float = 8f
): Modifier = if (color.alpha == 0f) this else drawBehind {
    drawIntoCanvas { canvas ->
        val paint = Paint().apply {
            this.color = color
        }
        val frameworkPaint = paint.asFrameworkPaint().apply {
            maskFilter = android.graphics.BlurMaskFilter(
                blurRadius,
                android.graphics.BlurMaskFilter.Blur.NORMAL
            )
        }
        val radius = size.minDimension / 2
        canvas.nativeCanvas.drawCircle(
            size.width / 2,
            size.height / 2,
            radius + spread,
            frameworkPaint
        )
    }
}

fun Modifier.focusBorder(
    isFocused: Boolean,
    color: Color,
    thickness: Dp,
    shape: Shape
): Modifier = if (isFocused) border(thickness, color, shape) else this

fun Modifier.focusBackground(
    isFocused: Boolean,
    focusedColor: Color,
    unfocusedColor: Color,
    shape: Shape
): Modifier = composed {
    val color by animateColorAsState(
        targetValue = if (isFocused) focusedColor else unfocusedColor,
        animationSpec = Motion.focusColorSpec,
        label = "focusBg"
    )
    background(color, shape)
}
