"""Exercise the 0.21.4 media contract through the MT layer and real scheduler."""
import asyncio
import json
import threading

import pytest

from hermes_multitenancy.feishu_adapter_compat import load_feishu_module
from hermes_multitenancy.feishu_inbound_richtext import install_feishu_inbound_richtext_patch
from hermes_multitenancy.cron_worker import _send_media_files_via_live_adapter


def test_forward_video_cover_does_not_replace_video():
    module = load_feishu_module()
    install_feishu_inbound_richtext_patch()
    result = module.normalize_feishu_message(message_type='merge_forward',raw_content=json.dumps({
        'messages':[{'message_type':'video','content':{'image_key':'cover','file_key':'video','file_name':'movie.mp4'}}]}))
    assert result.media_refs[0].file_key == 'video'
    assert result.media_refs[0].resource_type == 'video'
    assert 'cover' in result.image_keys
    assert result.metadata['uplifted_media'][0]['file_key'] == 'video'


@pytest.mark.parametrize('result_kind,expected', [('ok',None),('missing_id','missing message_id'),('failed','did not confirm success'),('none','did not confirm success')])
def test_each_cron_attachment_requires_confirmed_message_id(tmp_path, result_kind, expected):
    from gateway.platforms.base import SendResult
    from gateway.config import Platform
    loop=asyncio.new_event_loop()
    thread=threading.Thread(target=loop.run_forever,daemon=True)
    thread.start()
    one=tmp_path/'one.txt';two=tmp_path/'two.txt'
    one.write_text('safe');two.write_text('safe')
    called=[]
    class Adapter:
        platform=Platform.FEISHU
        async def send_document(self,**kw):
            called.append(kw['file_path'])
            if len(called)==1 or result_kind=='ok': return SendResult(success=True,message_id='om_probe')
            if result_kind=='none': return None
            return SendResult(success=result_kind!='failed',message_id='',error='probe')
    try:
        error=_send_media_files_via_live_adapter(Adapter(),'ou_probe',[(str(one),False),(str(two),False)],None,loop,{'id':'probe'})
    finally:
        loop.call_soon_threadsafe(loop.stop);thread.join(3);loop.close()
    assert len(called)==2
    assert error is None if expected is None else expected in error
