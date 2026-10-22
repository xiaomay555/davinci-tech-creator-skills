/* Preview cues derived only from the original transcript tokens. */
(function(root){
 const width=s=>Array.from(s).reduce((n,c)=>n+(/[\x00-\x7f]/.test(c)?.55:1),0);
 const segmenter=new Intl.Segmenter('zh',{granularity:'word'});
 function lines(parts){
  const text=parts.join(''),total=width(text);if(total<=18)return [text.trim()];
  const words=Array.from(segmenter.segment(text));let best=null;
  for(const word of words.slice(1)){const a=text.slice(0,word.index).trim(),b=text.slice(word.index).trim();if(!a||!b||width(a)>18||width(b)>18)continue;const score=Math.abs(width(a)-width(b))-( /[，,：:；;]$/.test(a)?2:0);if(!best||score<best.score)best={score,lines:[a,b]};}
  if(best)return best.lines;
  const result=[];let line='';for(const part of parts){for(const piece of width(part)>18?Array.from(part):[part]){if(line&&width(line+piece)>18){result.push(line.trim());line=''}line+=piece}}if(line)result.push(line.trim());return result;
 }
 // Source ms -> cut-timeline ms, shared by the editor preview and the SRT export.
 function timeline(segments,fps){return ms=>{const f=ms*fps/1000;for(const s of segments)if(f>=s.src_in&&f<=s.src_out)return (s.tl_in+f-s.src_in)*1000/fps;let best=null;for(const s of segments)for(const [a,b] of [[s.src_in,s.tl_in],[s.src_out,s.tl_out]])if(!best||Math.abs(a-f)<best.distance)best={distance:Math.abs(a-f),ms:b*1000/fps};return best.ms}}
 // Break rules (pause, cue length) use cut-timeline time so the preview splits exactly like the delivered SRT.
 function build(tokens,isRemoved=()=>false,toTimeline=ms=>ms){
  const cues=[],byId=new Map();let group=[];
  function flush(){if(!group.length)return;const cue={start:group[0].start_ms/1000,end:group.at(-1).end_ms/1000,ids:group.map(t=>t.id),lines:lines(group.map(t=>t.display??t.text))};cues.push(cue);for(const t of group)byId.set(t.id,cue);group=[]}
  for(const t of tokens){
   if(isRemoved(t))continue;
   const previous=group.at(-1),part=t.display??t.text;
   if(previous&&(t.unit_id!==previous.unit_id||toTimeline(t.start_ms)-toTimeline(previous.end_ms)>600||toTimeline(t.end_ms)-toTimeline(group[0].start_ms)>4500||lines([...group.map(x=>x.display??x.text),part]).length>2))flush();
   group.push(t);
   if(/[。！？!?；;]$/.test(part.trim())||(/[，,：:]$/.test(part.trim())&&width(group.map(x=>x.display??x.text).join(''))>=6))flush();
  }
  flush();cues.sort((a,b)=>a.start-b.start);return {cues,byId};
 }
 function at(track,seconds){let lo=0,hi=track.cues.length-1,index=-1;while(lo<=hi){let mid=(lo+hi)>>1;if(track.cues[mid].start<=seconds){index=mid;lo=mid+1}else hi=mid-1}const cue=track.cues[index];return cue&&seconds<cue.end?cue:null}
 const api={build,at,width,timeline};if(typeof module!=='undefined')module.exports=api;else root.CaptionEngine=api;
})(typeof window!=='undefined'?window:this);
