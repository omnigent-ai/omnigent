package ai.omnigent.android;

import android.webkit.RenderProcessGoneDetail;

final class TestRenderProcessGoneDetail extends RenderProcessGoneDetail {
    @Override
    public boolean didCrash() {
        return true;
    }

    @Override
    public int rendererPriorityAtExit() {
        return 0;
    }
}
