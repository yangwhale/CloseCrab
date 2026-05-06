/**
 * Live2D Emotion System — Gemini TTS emotion tag → Live2D expression mapping
 *
 * Usage:
 *   const live2d = new Live2DEmotion(canvasEl, wrapperEl, modelUrl);
 *   await live2d.init();
 *   live2d.setEmotion('happy');
 *   live2d.setEmotionFromTag('[excited]');
 *
 * Dependencies (load BEFORE this script, in order):
 *   1. pixi.js@6.5.10
 *   2. window.PIXI = PIXI
 *   3. live2dcubismcore.min.js
 *   4. pixi-live2d-display@0.4.0/dist/cubism4.min.js
 */

const EMOTIONS = {
    calm: {
        icon: '\u{1F60C}', name: '平静',
        ttsTags: ['[calm]', '[casually]', '[whispers]'],
        expression: 'Normal',
        motion: { group: 'Idle', index: undefined },
        bg: 'linear-gradient(160deg, #e0f2f1 0%, #e8eaf6 50%, #e3f2fd 100%)'
    },
    happy: {
        icon: '\u{1F60A}', name: '微笑',
        ttsTags: ['[happy]', '[cheerful]'],
        expression: 'Smile',
        motion: { group: 'Idle', index: undefined },
        bg: 'linear-gradient(160deg, #fef7e0 0%, #fff3e0 50%, #fff8e1 100%)'
    },
    excited: {
        icon: '\u{1F929}', name: '兴奋',
        ttsTags: ['[excited]', '[amazed]', '[laughing]'],
        expression: 'exp_02',
        motion: { group: 'Idle', index: undefined },
        bg: 'linear-gradient(160deg, #fff3e0 0%, #fce8e6 50%, #ffebee 100%)'
    },
    angry: {
        icon: '\u{1F621}', name: '生气',
        ttsTags: ['[angry]', '[scornful]'],
        expression: 'Angry',
        motion: { group: 'Idle', index: undefined },
        bg: 'linear-gradient(160deg, #fce8e6 0%, #ffebee 50%, #fbe9e7 100%)'
    },
    frustrated: {
        icon: '\u{1F624}', name: '烦躁',
        ttsTags: ['[frustrated]', '[serious]', '[sarcastic]'],
        expression: 'exp_03',
        motion: { group: 'Idle', index: undefined },
        bg: 'linear-gradient(160deg, #efebe9 0%, #fbe9e7 50%, #f5f5f5 100%)'
    },
    surprised: {
        icon: '\u{1F632}', name: '惊讶',
        ttsTags: ['[surprised]', '[curious]', '[gasp]', '[urgent]'],
        expression: 'Surprised',
        motion: { group: 'Idle', index: undefined },
        bg: 'linear-gradient(160deg, #fce8e6 0%, #fef7e0 50%, #fff8e1 100%)'
    },
    blushing: {
        icon: '\u{1F633}', name: '害羞',
        ttsTags: ['[empathetic]'],
        expression: 'Blushing',
        motion: { group: 'Idle', index: undefined },
        bg: 'linear-gradient(160deg, #f3e8fe 0%, #fce8ee 50%, #fce4ec 100%)'
    },
    sad: {
        icon: '\u{1F622}', name: '难过',
        ttsTags: ['[sad]', '[bored]', '[sighs]', '[crying]'],
        expression: 'Sad',
        motion: { group: 'Idle', index: undefined },
        bg: 'linear-gradient(160deg, #e3f2fd 0%, #e8eaf6 50%, #eceff1 100%)'
    }
};

// TTS tag → emotion key reverse lookup
const TAG_TO_EMOTION = {};
for (const [key, emo] of Object.entries(EMOTIONS)) {
    for (const tag of emo.ttsTags) {
        TAG_TO_EMOTION[tag] = key;
    }
}

class Live2DEmotion {
    constructor(canvas, wrapper, modelUrl) {
        this.canvas = canvas;
        this.wrapper = wrapper;
        this.modelUrl = modelUrl;
        this.app = null;
        this.model = null;
        this.currentEmotion = 'calm';
        this._onResize = null;
        this.onEmotionChange = null; // callback(key, emotion)
        this.onHit = null;           // callback(hitAreas)
    }

    async init() {
        if (!window.PIXI || !PIXI.live2d) {
            throw new Error('pixi-live2d-display not loaded. Check script load order.');
        }

        const w = this.wrapper.clientWidth;
        const h = this.wrapper.clientHeight;

        this.app = new PIXI.Application({
            view: this.canvas,
            width: w,
            height: h,
            backgroundAlpha: 0,
            antialias: true,
            autoDensity: true,
            resolution: Math.min(window.devicePixelRatio || 1, 2),
        });

        this.model = await PIXI.live2d.Live2DModel.from(this.modelUrl, {
            autoInteract: true,
        });

        this._applyTransform(w, h);

        this.app.stage.addChild(this.model);
        this.app.stage.interactive = true;
        this.app.stage.hitArea = new PIXI.Rectangle(0, 0, w, h);

        this.model.on('hit', (hitAreas) => {
            if (hitAreas.includes('Head') || hitAreas.includes('Body')) {
                this.model.motion('TapBody');
            }
            if (this.onHit) this.onHit(hitAreas);
        });

        this._onResize = () => {
            const nw = this.wrapper.clientWidth;
            const nh = this.wrapper.clientHeight;
            this.app.renderer.resize(nw, nh);
            this._applyTransform(nw, nh);
            this.app.stage.hitArea = new PIXI.Rectangle(0, 0, nw, nh);
        };
        window.addEventListener('resize', this._onResize);

        this.setEmotion('calm');
    }

    _applyTransform(w, h) {
        const scaleX = w / this.model.width * 0.9;
        const scaleY = h / this.model.height * 1.3;
        const scale = Math.min(scaleX, scaleY);
        this.model.scale.set(scale);
        this.model.anchor.set(0.5, 0.35);
        this.model.x = w / 2;
        this.model.y = h * 0.7;
    }

    setEmotion(key) {
        const emo = EMOTIONS[key];
        if (!this.model || !emo) return;

        this.currentEmotion = key;
        this.model.expression(emo.expression);
        this.model.motion(emo.motion.group, emo.motion.index);
        this.wrapper.style.background = emo.bg;

        if (this.onEmotionChange) this.onEmotionChange(key, emo);
    }

    setEmotionFromTag(tag) {
        const key = TAG_TO_EMOTION[tag] || 'calm';
        this.setEmotion(key);
        return key;
    }

    extractAndSetEmotion(text) {
        const match = text.match(/\[(\w+)\]/);
        if (match) {
            return this.setEmotionFromTag(`[${match[1]}]`);
        }
        return 'calm';
    }

    getEmotionConfig(key) {
        return EMOTIONS[key] || null;
    }

    getAllEmotions() {
        return { ...EMOTIONS };
    }

    destroy() {
        if (this._onResize) {
            window.removeEventListener('resize', this._onResize);
        }
        if (this.app) {
            this.app.destroy(true);
        }
        this.model = null;
        this.app = null;
    }
}

// Export for both module and script usage
if (typeof module !== 'undefined' && module.exports) {
    module.exports = { Live2DEmotion, EMOTIONS, TAG_TO_EMOTION };
} else {
    window.Live2DEmotion = Live2DEmotion;
    window.LIVE2D_EMOTIONS = EMOTIONS;
    window.LIVE2D_TAG_TO_EMOTION = TAG_TO_EMOTION;
}
