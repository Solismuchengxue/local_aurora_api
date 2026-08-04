#!/usr/bin/env python3
"""Run a bounded, sanitized multimodal acceptance matrix through production New API."""

from __future__ import annotations

import argparse
import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import io
import lzma
import os
import sqlite3
import struct
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import Callable
import urllib.error
import urllib.request
import zlib


NEW_API_BASE_URL = "http://127.0.0.1:3000"
EXPECTED_ROOT = Path("/vol1/1000/Solis_Aurora_Gateway")
MAX_REPORT_BYTES = 32 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 8 * 1024 * 1024

CHAT_MODEL = "gpt-4o"
IMAGE_MODEL = "gpt-image-2"
TTS_MODEL = "tts-1"
TRANSCRIPTION_MODEL = "whisper-1"

EXPECTED_CHECKS = (
    "models",
    "chat_nonstream",
    "chat_stream",
    "responses_nonstream",
    "responses_stream",
    "files",
    "vision",
    "image_generation",
    "image_edit",
    "image_variation",
    "audio_speech",
    "audio_transcription",
    "audio_translation",
    "audio_translation_composed",
)

SUCCESS_CODES = {
    "models": "models_valid",
    "chat_nonstream": "chat_nonstream_valid",
    "chat_stream": "chat_stream_valid",
    "responses_nonstream": "responses_nonstream_valid",
    "responses_stream": "responses_stream_valid",
    "files": "files_valid",
    "vision": "vision_valid",
    "image_generation": "image_generation_valid",
    "image_edit": "image_edit_valid",
    "image_variation": "image_variation_valid",
    "audio_speech": "audio_speech_valid",
    "audio_transcription": "audio_transcription_valid",
    "audio_translation": "audio_translation_valid",
    "audio_translation_composed": "audio_translation_composed_valid",
}

PASS_DETAIL_KEYS = {
    "models_valid": {"required_present"},
    "chat_nonstream_valid": {"content_present"},
    "chat_stream_valid": {"chunks", "done"},
    "responses_nonstream_valid": {"completed", "output_count"},
    "responses_stream_valid": {
        "created",
        "output_seen",
        "completed",
        "done",
    },
    "files_valid": {"uploaded", "referenced"},
    "vision_valid": {"image_uploaded", "image_understood"},
    "image_generation_valid": {"media_type", "bytes", "decodable"},
    "image_edit_valid": {"media_type", "bytes", "decodable"},
    "image_variation_valid": {"media_type", "bytes", "decodable"},
    "audio_speech_valid": {
        "media_type",
        "bytes",
        "codec",
        "sample_rate",
        "channels",
    },
    "audio_transcription_valid": {"text_present", "expected_marker_present"},
    "audio_translation_valid": {"text_present", "english_markers_present"},
    "audio_translation_composed_valid": {"text_present", "chinese_present"},
}

FAILURE_CODES = {
    "auth",
    "model_scope",
    "route",
    "relay",
    "multipart",
    "sentinel",
    "upstream",
    "timeout",
    "invalid_media",
    "semantic_mismatch",
    "dependency_failed",
    "other",
}

BOOLEAN_DETAIL_KEYS = {
    "required_present",
    "content_present",
    "done",
    "completed",
    "output_seen",
    "created",
    "uploaded",
    "referenced",
    "image_uploaded",
    "image_understood",
    "decodable",
    "text_present",
    "expected_marker_present",
    "english_markers_present",
    "chinese_present",
}
COUNT_DETAIL_KEYS = {
    "chunks",
    "output_count",
    "bytes",
    "sample_rate",
    "channels",
}

ALLOWED_PATHS = {
    "/v1/models",
    "/v1/chat/completions",
    "/v1/responses",
    "/v1/files",
    "/v1/images/generations",
    "/v1/images/edits",
    "/v1/images/variations",
    "/v1/audio/speech",
    "/v1/audio/transcriptions",
    "/v1/audio/translations",
}
REQUIRED_MODEL_IDS = {CHAT_MODEL, IMAGE_MODEL, TTS_MODEL, TRANSCRIPTION_MODEL}

# Generated from the installed Microsoft David en-US voice saying the fixed,
# non-sensitive phrase "Today capability test". The compressed fixture is
# expanded only in memory and is never included in a report.
ENGLISH_WAV_LZMA_B85 = (
    "{Wp48S^xk9=GL@E0stWa8~^|S5YJf5;Iye;(p>;45=84+vz;=x{2$$~l0k|8JWxlt^5^E>E5fW<cko;<aDx91iqD3eAGp6vdKmi>"
    "2m-f3AuI0A=oEoaov5!aQ#s(6?=erfIdtT#=st{G495U;Zu)vB4UFv1LL<WjWqlZcXW4vVhFAVdn*x%(9E=TU*<AdXf@?3|@Xbg1"
    "FTUh|8>DO<vmYIKhF>i*rVp0SPvqnqh#d^FQNaG=(q{}({C740R^*Wm$ha9T9Kq-Qazh@E`HDTaZ0p9&hxDDlZOD!5ZL%|n%E?M+"
    ">N{1Lq$cd#m<j~m58SL=#6%L`KqXT16#Q+O)TDdcoqIzT*SrEiY7#@4E=y-X(D<>Zah7KRJR$punbVCZ==jv0nSJWgFeeP{%g0L1"
    "jE@BoXJNr`*2MZX6*>;zT^j@nn*<zSU%1jQcr6z91E$6D)bz9a&5K;-+h0&PDyzaW?GTo60`dsMvIo0e&FNI&abE5=wUDo^s?aN@"
    "`YI7O>ax)aSRAncPot(-uFaT*c9=s^d3VCP*|Q@tXC%%Vgkk89DNW0&u32rPra?(BcD6hATPaz8vI1Xec=J&jygEs*C<H@E9_ffF"
    "d_4MNk=D9NqgDYi<nXZbT41Snv~)m*&v!U*Sc<@?Kc)-!DVXvc(h^Je#j<JAbLscS%@29QkA7>)9eQ4A{_R128Ac=g0JL!)1he7W"
    "4qYG*CW>#!qPZFn0=(eR`<^UHb9c?Y$6%&0fH_{`KM|34YWwmXQ>NS-8w7GUl_k1>g4$bMY#hmfvUryEaU53qmup%I!$zs8z^5W&"
    "+m76)XzgQE__I%JYeL%bqc%~XlpNj!{7WvZ?h+>f_6(1Rw|Hg%Oa8~_n7V^k#9qzn?4T^DvTzbhlSl)hhASl1!S7ct+E%p5NJFKZ"
    "V%IVo_-JQsu7+obX_X)->(CMDT4g(`jA^uB@ma94@=$kGcC})UVY^i+I|~4)PYx`ff%7puDQ*_COG5mKMkyM6RCH~42xVjqiza*H"
    "ZaiiOr#Opn!UxYb5`KC)OwiKxQQ85;s)Nt=n$YC=sHc)O*9X#|v^}1vE0mnhV!R~f62aWO>z4ltz1~x_-ZD63|J_eHN`Cn_tX@XX"
    "{%p%`;@5v)(Brf6{sAKD57vH-W6d3CM@Ce$hV5u#pg=8IQ_Tb4u&hBb6lw&u^njFB$Jv^%If@~afpDTNTgy96a2AEx#zzDD9AiAR"
    "^GhH~Z87>u<tUQIw9uh%<Cl2PXYMt45OISpF*kl&Hzbf00bmLadUjYCRA(<T+pufTF4kVN<ebq9QzH$625B2NqcLUX$R+N1mkrE|"
    "^iZ&5XEVfO+&!5W<fxZAdzDO@Px};dhp+Ruf>srj+r^@PaUE#7Er$d|_DU!T8mLU!3&CD!eTmL8Ta=hXAR?v2pBQkB+t$SJu-q(N"
    "V&qdA$Xx8$s;GuS<BiK)|5=&OuSSZzD9cOQkF(tsdjye#{r;`Qm%FK|Cp$V=Ww4$Ef>(qB0uhj$YTwr$*?N^4z_pYA$Nxpcqdqiv"
    "?ql8}%R6&ONF1KOt){>l-s-3Q`u<N6-WxBa8Wa45SA;u#Desy{@(1Kvo^lab4XlrPPKxp;Hqw&mR{CVsOK&)=R`(8lD#OH&Lf)tl"
    "c}SL{@i=*}!e3KPX4LZ{@~9E1+Du|Una#^<>D{jEWYZ}%#T|}RT}9ntuse;pDNup;MLe2uhr3k!fST-!my#NDfL3>U1{Z_s{T_yQ"
    "-I%u8JMEA0jLf!50sMN%$qE|W2-U3Bun&2f4|qs9x{}qI01djGgvM0g+cE6m*H0)dQ$5UOa6zvh(dGiUA3WO969Z-*(c_?9Qv-2R"
    "3;A%n@1N!)`j#>-lp+A?A6i}#@$>r1A`;p>qankLVWezPeE!H9bg7*|ok)I%nP|T7ukWBI@OVb~eUx6CNx6tpj#4yN88&i=?<(w0"
    ";IIgi-G}xq_|g79vit#;WhNmT1{e+6>EAQ-*mv-+Ds$jzb<_MXDddjcNM_5^hGF#>D7uK$Q3hL$Vtbq0pYkCM?l(Y9Djfkj0oN$B"
    "sktv2z)gl~LO~?r(e<-G`f4>kOyJH1>tu4Q?w#m*r^k1H(w+U>JC+b)*s}fOZ?to~Ity#JD>I`twO|6b0l=Ub)Ivd3IIG^^HZAH%"
    "wj#q&(z<3c`0d}g^huSo+AMyHDZILRiYG82IdX;6y4J-%2izeB$0NSTp+kC9Z$xRJQ-dXv@2M4qftgrR^00|&n|=Wg4?@MCOIyd?"
    "?_Uu$m0V=}LFDM^i~s}IaNx*?GG2OBCFKZ?ak`8%1iBU~I**-o6YB^8zlRMrWTDx=t5?khC`!)=Q`IiHOIEJL94N;w?)9QJJ2~(M"
    "!%rOj%9taT(rp1c^<fg0JXGk=C?;YZ&NSz|UwoJXZ7M9E@9f+3gA_(usFM<!u^yjrqSV}a^`s?iW?4iXLcT<L{ZLLhBgD@+(G!bp"
    "-kUKbJLGfVZL>@Hp|Px|r5jNLS6%_)2I78=ns<UZi{Dpj$?%}OZ^DTwb_(6s5T2m*>>u~)A90>#Zr6=bb8AGEqSI<w9O>0MkJH-R"
    "1PdVrM;R8#F9gXRMlq{<pcD^@1l%dofW(1s@Qp?KMq!BaJ0-1aL*dc_R0OS{<SV-VWAf{qD#`Hj)1S;R0lda{n0HECK%>)Jd}q4G"
    "97VWLY6d=zR1W7d6dE#t_;mw}sX2EYeMbns$k<6;#U?`%I&1-65`a{+<EW-=zG{k+3Shba8HFN$#`RY+EX^~wa35S-5nR=BNLtSU"
    "!TOCan{HmAO`2@vwI`m<Ts4JC7ZRg`H*?zNY@oGK!T%Nct{d&ApLDBC_3;wvfhleD5AX6mZPhf6y;~k=M|bYa?{p<u_HQ!>s3=4&"
    "=^5qAY0+Q+il96DFsIvbI9#oas<n8ikEb@}N$92e9!x?fBW^~uT^wOYninz>+~BNgLHE(P;1=0gnG1Ff!7E!eyu(G*SG06*eIl~r"
    "b&6JKr6R~=n=eLE81_H}A(K)xCa8&EX7*lRwoR@LTTo{uOg3XbARCK%$(GOP2SnB>l`RA!9E`ySc8jdm4{<(}jZpmvX%EM59-Q^h"
    "t$sSwu;`XiPv5#j?||0lvNYQ2so&v3?+p*ws*g7p_rs)w!v&kYIBgI$^zA(he}aK0<%f?!hXzo{1J7(iee7$E2TlXm*J|`C%-@&f"
    "ua;DDH)qz8^%hrxrKng-$(#Qo{|6`&q2Cv~JfN>K0$m?7JAFDIURw4vElanCaJ*FvC-cNB#<thOV;-m%{9=)VSfM@7)3^4yc$VJd"
    "yqO<s*lnl0n%vS`Wi$j6w4H+4E!Alea6GiWDfPWT6jK8;j6~^NT<o82$k8*CJ(Qs<Qh+<Ds2$>5Edwq}sVL)s4j_g}#3zOWCA`Z{"
    "4p^tC%!QdX&Dq{}_J{5zC_t4tGbWpTm)?U2^!xmpf&g$717eD*$EQR{2&3n~Vy1UMB#Rt~GF*CUXGpB)xluud%acR)5dOqJ(Tzfj"
    "-E)Xm5>XYQOr7Y2nv?120ItpbRf%*auVVAjFwC8brCUacmW%mXp@3sbQa_HC1p0BYO7r&yhOipjbGaNxVzaZS3JC?&?Bj0}KXyy{"
    "q!CpMegise+QCCtuaH9EtDpIBWVoG!1pU!%+`N9{O)>~)jrdz5NEqMdWcqhaP55^dl1%il(9Ea^{AxwrP(q4fZqIk;3rh?Zpw(jA"
    "(<>fDhSuIV@r@4pg@^}HMp@R*+3Y2<nP*H{^p(GtHk$B#hH&e&*H99bf^G2r!BLQa{IZ4~iNSSJtB%^%BRmke?$cDtVKDnXW-=NX"
    "BYIYV5H<i`mZ3x>l4-odZk#t17_54dQ${K6;mOF(b`Xcm=O*dYzTg~Sxu!))nGJne3_L#RvR`v<mpzShU*+G6c}UG_u;ev_o+Get"
    "oZc$jjtca_-T6q?)#SBKcOMFhQy5SD(Erw1k3r)o4gV8-n*|m{a*v@)6KAW$8Ae{3;SPw;&(K<o!Lzwr7bCG@Om45UvRrnjfYr1X"
    "5wl*w2y$Af-@Vd$x%%5_P!E;EZS!IVW8=rVv+AtpFlN8WGZt<dCvZ-q-=De5H?X3$B#I!LM2gyZThL?q%p)yVcm(U?eH<|3`+Rlu"
    "5A<<5VRgYhUbju_i{91E^`@UV@j-CM*Gf{thIcvTZG_Q|ph|n!W2qfce!S61phzouN4TMhWztt+BQ>I|^WaWtC?LwquFEQ$26cH+"
    "mJ`n?B+?wm(pLRkQB`B^%vBO#f+7`S3Y3&1vU&Zm*Y@z`rpSLwR|4dhCQ1bS6?`M2i44(9YS9fid6`D@YmL+e2>(l!)u!v!yoy?Y"
    "TIn5!E~xJS?JGyKk<wRo&NZ@oF`F0e%}r~rN7mn592H3}Ep_YB#sSB=ftr<o@@TIGVF+0$8V*)W415IkLmGmaj=;@)US>Qp53yX<"
    "73!W3y2;DB340zdO_hx3pRB4vLYOsOBi>0U{}mKQSe8F^?fcAmHF)LA;q)k*H+VQtgd&KKOs6bymQ1og8JyvgZ`@ACh%f#Z4fJ9t"
    ">cxBC!C`Zz|3LR4fl9o9p9;QFBPG1(*@?(ipkJVUiFXWt{0_{n%Xt(6pMgOBEI~|e`Ax5~Hwst=l%v&1Mx{^<=?$4Y_A@ZJLslwQ"
    "Uesdv1^puf6(EnQ<}n9+$?%#hWDEJN@jthcuyp5Rl|hs#V!ltmU{9*=j#hA#)}8pK@ZG-OHyya=$Zfpu<8l>@aA;gbIg+khzT+k("
    "Am(W}ij!%tBg+Iq|5*biPD0Q5Pq>g>>@{v;myfF`w$GT$5;6eD;Db(E7c*sTvXPv_@bxc&8q-G#Zm<vlCZ@O%Hm3L;d(s|6YkhzG"
    "Fd4`Q^}f2IR(edSoo!tkIXPz#*trW8zv{e>05}2uG0zUb{Gkr%KXbj~6L15nbjyMg@@HMw8nqL0SlXHzXZ=rv4RF9YdlIYisV;Vo"
    "=d2t}M)yCqDXLJ4%{>#=;j)mIJFKsD!BbtphADw7tW9)&r<iXZ!~3|Eko(y^GgFVrNW}nT#uFgI8-WIHQXhrlzi80CwYG=GIHHS2"
    "t?tKauB-T5Zo=j~;C>rUrw4X%U+oaRzgCD!tN~hrd7xFL1PEY(1<7%0KialWyCf0ay3=7bXT}K7Eh7h$=k(-=q>2mMp|h`mrh};I"
    "-0lIWFDAvyAjYcHvo*j1bEi2ji(ZYdpF(nj;JevCnzJPQ9h^<h{XKx=geNGbI;iWj%%2;(>or~aE4YcD2>vRa3!*I3L2nCyWHdwi"
    "YLU(U{q(i_3xgfr^ZFVBjm)~}WROQEu}av!#Fcr#TUhsKUVeS-ssL7poZpN4gJ+pMzKvwLRq?wudGl4L*riozsAtM9xY{OlgIrD7"
    "o`h6$b+bBN-01T79MdB5(KCs}f0O|%O`K9|K(x;#;L{!Ohu3|E6jTlf*B#7mSXAmO9lOxuL7@KRbro~a2!$oAf+lm&NMSzQc_gC#"
    "QEpj27Nm2QI57<8+?F|~$)B^O9iIe<!x@_IH%QWGK1ap%Ttp0ACee=+oXo-eCiI_~Gu3!4#tP2b!{ivtX58cdfdKLD6Cvb^?Fvna"
    "L~i;5<AY6N4ChyV5MN=EGfZqQ#Hvg-eN~x9IheG@))&@mkp!)xZXp;kU>*%+x!GNlS_O!4g;zL_yjsB-e8p)s0}1iBd-VQSu9t5-"
    "mJxcD*~F{iMWo4mcH%$1gDqLMhFG@aPKubmp>Iq1ksG<iHbC)o-ePLI^m7+V*GZi~m|vvi-p#>3U2M<xq9)HpJ$pgi#kj?0A8kSJ"
    "rS8owbka_uWQ&BZmS|xCrgQ$8c`-QTyQQ!`r^b_F<W;TiUxb#;4Xh=55ndTB0R);YDm~C@TQ~*sype-=fdlp3)C?fPv!6$%4i+8~"
    "6on5e)1@H-_?~A$Ly5d$Vg%pxfNK7{-B8JBrX$~7+{^1JoAfk>Z;a$~O`~1|wlVaW!PbVFXtKzM0<yVoh$Q+!7Zp<Qa2smNE+(5D"
    "ww&R-yg4!&oizreAH^qJjmS67ufKM5f~_sI`-y0UX5w;J+mLy1$Z1TzgQrylRDYiq`~<TA2lW(VV#7qa$h|M4Ub36ElX%3q)w*EU"
    "2D1<urtnA{^G^b25EyO)?wec7V2C<tjOlo-73JK$NX$+&fGc*bC^U*3WfW~2oU8uQ{U9<A$eb7OpNDzt>(gp2`);sDKQCcYAF4y;"
    "%neBgd9?0EF%2tLAyjfPsbg?5LQOOV)ZHod*9Ol+JWs^E!ExLd*7dK>S#XRhYzbw&mYwF}=xq>h6cPM$ZwwKiEvb_Rrkr2H#l8SW"
    "mRiTsYqGO?lQ_WA<GCL&46|HsB|%ko-PFau0n6uju=T3)Q^bvZ2Q0JF6FBXhl=Be9^j595P8f0OcsS_mthorFSy%w?sgY#fA@mx$"
    "NsXy2D1xmmt<>u7vVM&v04UK#DN}8w45q9^1SNQ>vqa2ShnwwXBu4MO`6}?b-%}hHYZv|>(KD{<RaIXS<DP^~_q%#RUNd~whWSe8"
    "&wD!YmgCh(Z>orf6($0kc}!WzyeW=q4Z|N-tUA$+cGjzK8OElZtI$RBnCLA^OQ$Mm8koZP$whUN`>ck7MVBh=pr_uR&XUadtegxV"
    "+~u9VZT^%r?#2JtH!Gk}PO-tz)O63@by&lwA@K?MQ?MBBH&mxSp$6=_S!^M@7{6<k1-j)AJAeUpL*V5sy9E0*;6ah9!ki)TH1+b@"
    "qVy^SgfReA#s$LQ8C=ETLE0dSYlu0rX)*G^e6aj!wHT1BRymP-Z?lUJbW>Io-ZZy>p@#3_)dp+eDT*elgjl!N%Pl@8o+agV7bqAE"
    "g+R_tR(|0+cHupzV9x{*;9xN7{)>tByTZVIstf5UXa|`5lzn7aVKF_dXn6^6P)A>aA~}WBHlzO9$j{)(oVbzd6<4CkewsqP>}3Vm"
    "aPoRyY;$hL|3XbDc<(lHVf4A1+fw7a8-!)p2?VbKBJJH9vf(<sU~C9>q5YUys_w+gU$Ttzdw|%WVFW1FZ}SBFEd<#teB?_jRR|rV"
    "0kME~m1aI1>RauiElQAIO+5yb;|6h@*pXl4q1*(u+5ej7E_-(AQC>e1gfLmQBwDRC0+{RmcfTPPVprr&Ncl6m8B2?*7t7~9Z<})r"
    "24H<ETd-0!v@i#30En^C28)`Y{~L_pTsyQs8zO2VkAW7ouZHx7GWRTs1i5}!<e}J(yr<}45=K=mK5)HwkX=6m8g1x{^H;{F6^J_S"
    "t62c(e{(pOX{iC9GkVpNtEoOSxtI_wnLgs;X4%WGoVmMIig$b2*_Z~Sv^eL3vKh8d@up5c5~R!J-qyN$4v-g*mgBQ#+Yt0qpsbt)"
    "4E5m3FEh(-l67%ca-C0y=_jMZo7`DZnYIaXzJ#d~{VZQ+ia_3B*hzg(J&<xnQiTzm%AKUl*sqmL1r*)V_?2!AfeG|-!G4ro=wPo2"
    "Nqx!a8I)n%JG+>Sqv}K5_mE2nB?~o~31!%`M&+3%ZMXg7!I3=tDU%{H)+y(6@I_!uhceO6q>jtp$cJ|x&<f0&Ia}0@V5uNs3Ku_)"
    "fOfMrrk`m;H=H578LsyAoB<-mR=iL{4+>XXk?>7)Rb3O9wMxc~zyj$Q6h<D%=Yao&H-Win<Tv4q!D2%9M&BcPcEr2tGEi588Kpfs"
    "<u~#xk)mJA@zl?LU?8O<P#Xe(bXn0inl%|nUxn#a1dbJpD|i~=C&h3s_gkR=RvEil38Zy#a!5FG88k7J4W`jT??63OGY|C3tW9C<"
    "-~VUD)o)CR;*a4tLNDc(Ia>JZIDI3WBt^-vgim<{MW4+5sKH?twl=t+`%IqPIPie-gE+p3Ide5_T$>lAO{g;Ip`F6&;v5{-iEI}&"
    "{4~k%8?55tB;1{z(*}cC*_6O>J;}WfEusI_-<@}nTY+6M{<pwI(Q@?N^ZyVj(`+&cVcE0Li<mrkm4c_q$>F&y7!s!Ehbfek5IT*0"
    "+<t^!U>Ly?x$nLh1p@(O(UZE{lg1z3r<qi#%Uj43F)EAPZwF^nbDHjBPZ2ZbaPz(Gg`e#OPcxV*R%Lc#tODR^?}$r#>mzzEu9N#{"
    "GXbnX<ssIFsR~+@!R9pphRfB{|7SJDU}q|k40J9zK^e1mXVeNYrn7NF{3r<38<Y)1Jobf%AVSmh>=7j_%UCaI%?}T%8lc|gaEX<L"
    "gOCvaeU*E5p)z>B+DiTH)!bAiu<iQe+W--&>Ze1iNxXTh2aH^geydXlxluNr5mD{&#1*J}k(2&W*oA12ltfy8;3&W1PxcCVH%(kH"
    "N6Uvq&-7w00!Y3+1X8N=k&M<zd}Tb0u$=GCBSW*cMN^Q7TA6f0pejbJu+E9`?*e#p33C@DX+UW-)eem8F&$(KI5%GwLdi14%g<%N"
    "oP)YUH+og~&&dK(J5;d#CZ464ra#$p?3k;eNEuGYWY0<M@lcqi_pm+Oy)@Ein|%?_tH$rL9TBaV`+WNLR-+o^NQR`14Wt$lqR^x^"
    "_9;K4CCxF#=dC>mast!!AeHvmz>=o^os{8qhq{E%LB?Fs^$9{Sw)tTAMwtQ0_@)W&(5O=w;Gs~z@s7YD48V$-;vc9JQEvzC2n$^3"
    "vul7+21g8O1VO>Oh@2h}YZ(9!gaxB}^+cFpsd=XrZ)6QUj%f&Jd#zp42W>4nh)zv0Tz@vW)ei?jS2>1tMS2-kjKNq2+P2o2N%IZS"
    ")L(|yXgoGPH3qp~nA#`)qHlB1g)r_A=MrB|91|JL`bVQ7Rs)_GPk45ARI}KkCvX%aa*)P!>`!Bk>qZYAc<Z_bAn}a8Vx!@AV>@ym"
    ">duQMUcIHq6pwtXEkbJmi#?Np0kB8}bF7pTeFJ0au>;u<ojZkx(rX~-`@sYW9T8?|ue)fdNl4B*Xp@oz@Yk684I*BIPw4Bexl2E@"
    "U#Rv%IeS2oX}>*E3pu02Pwa*y39!Th#kMHi-%vSBfN;V)UwzR1U9Bw)G`<2~@{Jmhg?e?gf5Oe)9lC~TxT*a<4I!g~OgOC%h-c>r"
    "4sz_e+W%VIe7*^jB;<DPvEpp)x>PyJJTKaqO~CXv=-KzFlnFmz45QqYot;z=_G=faO?AwMGis||IM9sbHdp|0Zf5|mF=#>)FmhoJ"
    "xGLPC@%AD0aB~Sa$hz<G^qB&ukq7p`u}N}bVl!8=lO&U3!6`z$NnKN2M+ikg%K-4F(_ePKip=1i<Q1`#b9uf!d<h*UwcybK!J?q|"
    "fl?0ukBUy}JA7;o2)cxz*q2dvKIPhaKNq^BD?)&m>ZxT<O@`<#U`t3P?Z(jjdy6NzkpC?g-+5tB2n@WaTeN}`?kZry8D?rpH3BN1"
    "2>Q10b3Pkuj|XFT?ko1bx2Nr6%_Bc_{_B&MO_LAEceap7z3~Zf4ojwue<9mI|6_U)HAK3R1*`2%j)3icCR<x3>n4l7N3U*r;g+k>"
    "gp-fR8#qjp+RCmX+^D}ouo;BW(x4*+*3D=Qqvv0PE16p?mZ_{#G-6~`assB&?azWe?-!N+5VO%C^}n)QH!cTQ9MLuX1DZP)!v0?o"
    "Cv?O_%_xFV+J$&2$*$O=!1x$pCOv-@81jK<&j<UQABwna9b-SrzqdY}4`v}rU1@@*`P`C)bAm^lToOo;4i)F`D?hPgRVBvBtm%(z"
    "(dDi&0+;s%gI``%&=p7E?^nTWODBWX1hwwVW6;DM!<&JJ@QGvhYR7hT6a>08d3D@eD2p@32Ai~~K|b=fg><dg>%Os$SV&A99^(gT"
    "W&_@$FOW=e(W<eJXwT>qO*eB^E-V%yQ@5#T@k_Y(xQVA>G-z0{W1C15oN)5zrl}cZpy6ZmRW6qTrO)aqtT%Qf1I(^w2Fmx6agGZD"
    "us0i`-m@Me4mlBJHbGTG;=k7mIu1_$(m(;jh+}+;S_Q}sT4KDRZ;ngc;gJ)u$aHR8aW7!*uSxg$5Y29{&hKCNE%I!G`I54o5>*<O"
    "<NLf`uv*Z<^2<Zd2I~P%yF9c@^L4C~Bt}cuILg40hSQ*VFx>*+U9fmE6I2I6#J>Y0n3kOx{!k&Si#jh91oDb^YlCIAP*~#TcbE+{"
    "5M!pRi#}m_dCnD|y>}tHm*S4gkRD>U$~}+qD`2<6d04sx{Kc6qZa@@tFj+*7&s-9pN7`Ko<W^Wj`84}{3)g%906}>X=Xd~~(0*V!"
    "YVt(#-I>NftzxmH%10m+-ua5Iqk1f@kZhCyvaAa<dCLB;rF4LH!q=RICHZAlB!<BjZ%(U|g1riY$)F=OEA=F-iiVOo!?dtZi>76$"
    "6DFuQN?-dDb1TQluvg_c2+b{)9>Tbhy33K`F?d^tCfi9Q9GU$!{I^2aj^*W8<zo*)(?|~Vo5$5b@)zWVNbO?|h}>yH8RPNMk(2$C"
    "`{4~wcxM@s8NWMyq@FTDAIi}-ayKmm7fK{*6V;#pYT2rfsW~~(S6w^8xDjW@2)=udEY$HrCOO&VRBIR(6Ps*cEymRu&X(=bc*-W8"
    "xkelK<G!MmOZTYXa#3GPv>jaYZH1%s!j%s>*F<jyjCz#KLNR=C^7t&!Hr4Ozrg5gW5)GvwoSw*qp4NAqWt0AP$#<hNCoH)=z*8>H"
    "NZnGd?Hn0aIWeiHuWjcZji`)w0j#Rh%^4!Wzj3^&tm!(n)%9;WuAs8}@=sXFUQ}yIo~cWmP)OjugW38*9l!b|cqt|mgx$9R-5Q{c"
    "it2#Ag0Z>ciJo%<$~)P>S7i`+HPDduDq&XwIG4gD*c?BFS3T4|6%+X)LMvZek(munvB3NPrIh-VFbGLKut7XVMcO_y0I-EBu`;G^"
    "#+(M37|_$v&6%E)L78iS(tq;zmedc_YBOdgG+_iK@-AI3*!8K;8_-7v>A@WMUx|WIP+`nSva4}(M%AH}m;>2hmJbQ}Q{qh%{*_I2"
    "Zw0&PUKUJRyOb|XO@pCo=`#;2%_k#R{yqn*PhhgKHfjD=a%oIwTU(P1SX1%OV>!Y~BsIP|gh$0}ma3VVhX{>3(~za&hiNFxIbvH="
    "No$&`7r(}<)mGBSJ*938zPyM-bGO^xca^iEt7~;Fq30394;-VFN(?S|-(=p`N0pks^B}=Mb{?F_7yd#6AHf+7-V^IgnL7rk0Wgy;"
    "5D3RnU|2FE?KA(C-_lr8hWCYWvnXpNlIX7W$~$_a43UBPw9LrF(>-LybwJ}Y5!xr-U~sS27qNf*JFw)yg;-v=zJkK668L=dbX7>W"
    "Zw1d0pB(>gIfeHos~3E59R3;p`?}TqrPX*$**Fpj-G{>!z<s!NeuI3NxCrj4pE%eu87|`cRo@<#Su+F+i?H<WmQ7?rNJd761wId~"
    "6rb=Li#`2?iCWElU2d7=fIoJ!hF*lQd2GqISJWvE(@^d-tU8@vtJ?kvX6fSnX>%q4b~6Mo)mEMho2$#0+PQUyKeS|&l+HFs1E>w~"
    "1d%)Oi0aySE0Wcfcgd%oDIZU{5PdmDQ$6g&--~VsQm0`YAYZ1IVBQUCJS@%Qp+GigKG!B5x@Y)BTsNwb3jqmL9DbS85*=G0Ay`~~"
    "c?qlB7q^1SUwUKPvjKaKNcJ2GHpmmsTgY;qnGN*nIe8Mc&@#;@AhpFfSWElE8LY@WRaIxx#}<=!4u&mb{^ooOpN7ASb?a3<D+rVY"
    "o$lotBQJxwS;V-lm3IpTx|C-09h?}6EgenCGi<*p+zbZgce`~mVtV2xYQ7p-*U#9EffmKQRDPTO>;<he9{HO4259DEnWcd+W&1{Z"
    "jD#o=@&mVAT%ESRw1YLS374*2wZ~-e=_{`{f63u~UT2VL|6q|X1bM1T9p0}UbB=jx?6ME=X&eEaTMqBF6COlA*1u19)XzN58EF(0"
    "#H_j-C{3n*?dzZ2C_<GdzcR{pCJn{MS6M2Rf9Cs!I6@_gntHM^<UUtQwR`=k9IcR^-4Yl_aNx0Z-7xF_{@BsR0UpSzWIkZ6H;N8E"
    "$yC;QQg6OilS@oTu|{Z|^7gJNIuPD_tZrybsB<P7l>aqtYRRR+p>2!@E5hNU3WGIye*#~z;`OsweqPgJ_yv@0*f+?M;PbGXk>cG^"
    "xJZpMU@%u0#_}^S5VqE{Y%&GrmX<@v+>tx37}rRp`8>mPv|uc!E*bR}aYdFkV=ZRD0v{+PaVcxl#<NeOVH9sT_z#h-T-?YkKa1w_"
    ">N@G5JGNLuLjy{qZn}brwbZ_UYYwvU`BNC)r8Q<>aM#EPGJ_v|JW2by`gqvPf?x}4UY+7he9;t*!>$I0Zlvj5BtXAFHFM~^M&&q3"
    "dF*)93k1EPw)fx+?;|-a{<|#J;clFgU)U>SSU&|eu()n{{Hf%>1yk+|e&O2Tc)duJDf<S_{^L#Lw1y>`%j?wPB^yS#19WhYa^D!F"
    "-LLRnZ`NTQ*!rM>hrd~(U1ltOsM5McB!^>@(U*f=ycn2droysTyZY$aV4*{1J5R3=AR0Q?y}hJ3ZaX)LIO1qdYasJ*Ji{E6rHSL0"
    "y3H5^`RJSlL&3VJa6oMJfDP}X7M5<mOG)nB)eqZcEAKKmneqW4iTiQ#=rIYirG|jIO(4MsBvk2L*NW)yoUrkbCkvq>aoIG7*EB$r"
    "q_C&Xdw+t+3PyH>RV(o$s@Kk*PJ%oWH}8BErO_d~A0V!~F=P#ZZLWtW*GR(7IxGSRVxF}Vvkz4cyg6HcK$kIn$>T5x8Q24LY$d5("
    "mJ*!-Q3(RMh0ad^nwpN1tc^=sbjJLqt-N){g*pJ%z#BF*_}&0AxR=#tH}FF@x$)^pJg>?D!B8YDT9WATB+TWoSd$I%|5AKnu0MT)"
    ">>%(b?5ut|PoCz9H~(d^i3iS<uk4l};8?wVySS++8XZn2)|gY#0=~fhVUJ+XVajFeqqv6Aep&O@r2OY~gP{ws9EBwIZR^D<U3)$n"
    "1D&5cX%~DnM5aRML{Jr=1_~>q!R@8|LHMN=l5xe#KtT2>JEGUGY|e62SNTBuGvStOg}_X&;a7biPk+fq=bQ&Y!$A|vJbw}J7vCCz"
    "R|zjlRBysbxt6?)87E4OH|BM!#aCDjQe(NHkspXAM|z0$&h}b>fC)%!j5rIE=owxYwCztl$g;<41Qu)ttUMk+VumZ!H2iwpK`Ucj"
    "j(0s||Bt#i5|i8gMaU$IW!#h;JpCy^7r#!F0~>0X^@LJ#)t00`CH4cD_y(h=uKwsUhN5H3OahLz1nxa{V7ykj-eloL?@wAq;N9EU"
    "X=hkEQmd?Z$PCHbKE-ZT(P|;{Fh9)n43E1R)4Q&pR>p?n9C-vmMtb6gRI9F!P20pxfOjX5oe3c@a`S4ORUte5;5cMal5@qEL8s`n"
    "3~nAEU53fkngH&$JMi9?d?W9iSk$-R#F%HR>jg6WmRaocNa00zkqo0XNmg<_;E)&ZL->=nlLAmhEba1A_h=cu%i#k=nA*sBDYg;7"
    ">zn1mR9r`l2M;DN(*OmC{j{y*MheP5$e$1_m7~70<&y+tl0s5-;Me{9c2q<G=Sa2^?{G@WB**)aLN*)-<-lu3uMpg8vm@p!bSdQS"
    "WU~`XC21Xz>4Rd{6+<D2DO16@`=;eN39;A4ipo{8UPsaA_ikTE1v7~;fUvE226LX8f-AnU#AhC|aw{AG+cqnE)P=bmxmsbutw~1g"
    "*a6c2ZassVmuv~NId=kHvfS?bHctik`}#7MXku1ii}Sxib}newBuY4{<)+=Qjup^0bTfZf@NpXqX08}36ZrXZm||qM6B%U80xYnm"
    "%LxM)q+NRyBtz+_`)kdh-#Kr%q*Geum224wXvUi`uV1f+nv(9mXEEq(i#H*`_nCF(_#Yr@Jm!fg{bItGa$1@xW`F+8v^|mK9j@1v"
    "_C;#DcTew}HgpfY^s35vp1kJH_Kei_Rl#Mh!|B}NL?q~lKt^AdVT&VJe;*we{mvo*4sEIFZeuCz&F(K#C<U}A1hH-9fE5JOaP8!+"
    "MhscvRd?6fiIQ*l7>-Z~+!RfZDJ@$;t~@*V+?k!T0nhYDy1yM2&>Rz`Y-z*1MbR`U<7jC^p38-oqYHK)IaBR!I{;K-P;nQUms9De"
    "<+TK&a8T-VJ5t0mZu?RO9ULFm9myC*@W%m1SkeVp7tn*l0qoFZBItiEY~X0`Nf@Gc;3>PevIuA-vhw1B<hAn~p;SK)Q1=c)F~}k4"
    "*PqpJG{@za^sIW+M8M!(ysrcdt*5L$0EG<Iojj(O@qpTaIJ^y$L9L!Url~o=@d&vPTL%8TxzS2MfEi6{P*MGgb5e6DNO2LxVmA1*"
    "c{p`Qymg>ZM4%&)zcTo(oHbUnlfOPML~o_-^EO&C!x1p(BD<7W<Mulu&uBK=R7vhYQH6u=&9eJhQh_&E(;Ny?)UnaYZ9;$y7)P+&"
    "R{hY)svcqY1bSlX+yd+w@a8{Y>Kgv`LQ*pxg1y}cHuxf|rlM!&?JB*F=xMJMjB;r?W)vli;gc{6k8gteK6c2fz_HC#OTuE#$@_+V"
    "0rur-!-ICbKFyC9>kBC;q(80qsqme3xb?A*Y4iktblcqab-;I-)ong>y>4-J6iE*kxS}ny)}4Bnoh0qfEd1%W5??w$)?lOWG?O9m"
    "^EFtejhKH}&E!*5WA+*D==G=Q6f;*XD+YjC99oh|LSk?*ft8goT2b|y58GaJ4qxvJK<OZ`43Z_K^H441o(wh)QYvj)D4F|tM0b>|"
    "5i;x*@(Qs4#!$Q?!d^C{%xB7(mMEN(G_Yd!vwsiSO(=~c0Z5MI89&NUARw!z4#ZDSCq=cvo=5oruIq|2V^iP*Vuj%`Ey!U;Pm1S$"
    "cjsN{bvzs4fX0rR$0>~tw{zmwXjSW19z`>rLdvO^G+l*z{;0=886CaXS~UBLg}xosvyYbthP;j8B~5OST(fBy^Fcrgvucj_cH~dy"
    "q_BQJz<pfQvljY<@az$EZHPj?Pk-s5I@+_<AT##oo94XN0Q)mL@HCRohu<9<x!iQxDPY!T2_Joq9w0nqd-0=`s?H4o|EeM!(r4gW"
    "wdvQx5%W}GMA+etne(yzq!0#9kelmPvIUcvMtWu$g}0%~`^yLo)n6B>tDEKQ*o+6SBtoW`boJ<puSBI}dB?ae9h=;mi>>}7oQ0EY"
    "Fx*$LQRDzBkJYA{2K1>?QdYba+U<i^0kfZEFCw)_6Ybpu4^$b|j*q$k4)5k5D{Pr}@^dYqh<qb|-{AR3nMWgcfZ70bWNxdyk^Vc1"
    "Fpir2&&W%^t_NlW=z79dS=~1_`<kJmm$~u1Y=v4XBGL^r)48yJX+Hz8*R0<M1f`)kLQ9#O_7P|}KR}@9qZF}S;}LJ+<}O}$4d5Bn"
    "bUCP&c>7jsi?ZHPtHA5s%J!ptdu|51(qA+;94z|)e1zjm;ATgi)V`iwa8epvw~&%E5M2Dw3Fl=ODYN^Bf*0}wsoinfn?9U$&391M"
    "#T=<8s=H3aoS|EX)oeSueENT7P4b0nSqWRzJuK2K`l{0Hbi^@%V)UsPhw=s-5^ajbcQPu<S2+C8DF%$Eabb3G);qEQg@CJE|F+a^"
    "%|y&w6N8qe-W>((;E*RHbNWRn`-lxssnup!Y8E6#l0F|`+Vz}zaSzLI{)6k1>O4pj85J0kh7QXFO}~w^-o<if2hqW~`~AFo4Q_Te"
    "Iv3k<oh=|A(z0wNvLo#(8-jpv-OV~%%ND9W56RI0-CYNrNzgIpA<*hvr0Iqy;{)4jF04m<T9&l`po8rPBoXzdOpX{Ud)jZ$8+1xL"
    "n~Lgc%J?Fk&8EU^$$XqH2{`yLq2$MvSrrIC=I4e-U#5pyXYmaHhS5CW7-~@*DF%{(Wjik{42#kZot3o{C_o26j|(lX@kH-I`SYwT"
    "F_{dn1}C&(wf(zXX0v0pw4|yH>k0|$UOWs}baCeRNN!@SRg5P5)Ee;N5u(<01JEN$P$4g18+Fl9L&vfLp6Gb-x-)764ay{_kEGV)"
    "Xs^4jx^K29l4R@=>&*EosvCW><c*1}_jAee!=I0udMEve$7$C;qXIckc@!aNCsal=%oTx+RT5Gbsf7Evra=m_)-4)rR>BE%P#9?1"
    "4sVHL=_7Bxuu*uUiCQ?g?V|R1Vi>OEf1gn_x^qjjs0K!sNM10|yqbNO2TtnVsf@F1KV3C8Mu@f>u5oj>*}+XSW7v%nZqT_!ZN@%&"
    "Sr#Pg?e+&7-|OhuBu`<DcQ(oICpHF17S9`ecVIk6u0@|4WdKAf=0DKP8xGlr#&$chYw)HwW!^)d^+5*j7$Ug4ISN_#QTE0r#~MWO"
    "ArA;PG}RCQJ){_kp(TKF!gJ4~?hzf>c5rv)!(b*GPE1j?VQyZ&20vP`K&1OdhXUs)M{5u|`Kl!v9Ah1|YOsp(uIH^A9HI9{r*66{"
    "A5W^EKgL|i-JvR1LR#&{7zK0q64DwSanE-Ha6;g;a2@O4n)02)hxVL=IwC&ce3;KyWGk9TT`aTC&J&3e`K5Rk77?M6#z@j=f9JpH"
    "9N^g!ZDyk-4n*ncg%))(jt(F0OMu*>z7`;x#sc>x7d)tQ5TC<$W;29_57w4shT5B#I`E@Q@sdF+cPgs>t9N${T8X@Sz4P7*g)1O&"
    "04v?4=P@O6ahk(0hf_zgsCo#*W7regkq<?rmMaqpBQLA!Q{amK#+A5zg?AC3Ql;`NI;pG4wldefHaVe#m{v`5lYVR0Y_o*O+ZV`K"
    "NscHV-MczL34Ip4!B72`#eLhs1VKR|A>fs<a(+pg^jw4Bg!6$^PBAA+=0sbG_Is^E1)P-%#f#nGb7K9nwYrz8o@L;ru~)y4T~nH4"
    "1o&MdM)>=ie#u5LXEq|L0(u#Jo?B~jdOF{dKO_R2*x(-ie=27g86J=5@J+W)z31~Rs1ICAqiYwYe{O{txPqenl*?U5^$CI*sV1I;"
    "qeiL13a|Gu?^LaL#ms0m0dboE;yqmF;%W9UhbrF99;v*5-D!6P!W9BBkaXKD!ckh>&lc~Qm6U*4@dLas%PSy2wV05?{FiZlwB>VZ"
    "(cbkwlXnT>$djq?-Rs{=7W5`66DORScJt=~{Gyb%xa{f0;C*T}#wcn1<>Gm?+@>H%Hb!ysTo1zty`HjP&-k&HBzCknQY%g0ARbq{"
    "Yu;002DQkM7Kc1^Uful&J)ErDq}4Hh&C3M_YK5K@1MjYXbO=u;3s2-6vlqLY*ioFqHl_CEqQ*Pj7?L(*?Tb3l1g-1i@vu5OM#6oS"
    "wH{F<g_vn_-~2tD-&^Z@MKQKTUC{GiFXdjf+*P$Gio0gpJ0BomJCp$Br5FW%o(3Lbcf??V%f10V7IYzX|G58{a)mY)-*osU(`gyq"
    "D5+iUHm5fFweJCD!!{4Vz5v6Dn{=?s!bjV@3rSncFZ1SLkJJ%U+@$0zrZr3)!;YvHoFNRUm^pqVo|D+eqF40Mr#5jN=Pmw4qu7E*"
    "tkTsI*CmUXs24%F<s4bB*i7T@C4HC%XlHCd_<zS%r^fp+YkDI0O1Z9LRPzFST4h<KX1|OGywTUh<ZsXaiDGikZJ}<>@)1Mx*|MAV"
    "?fik4H`f5Ae3Hv?Bx=^yb}>TTKyvQ}#>X|gQ)B?QWo+FJF6`9F&!aLWV^C(&)8|OoylV4fx}5$ksFSLn+161VgdRpohru|)zzP4x"
    "BQCX4WBbOL{j|;_OvzX`P)3WY_o(fgXp&M^f&5!x31m$G`n&nfEct<pWlpyYs&(9gyh1UY9V$}81-4Mo`k2|wgxSKCks)aPXkMs`"
    "lN{}*&J7R}EQc1zGOc!gqaU+XlZMGY3ifKnc^qK*%?bU`rMLS1Y(IGf;&dQ}U5$_dCT?^A7FOXfUY7bvJRk-{!4W4#v4MuqGSPjO"
    "Lf<ntyx!+HM12Es^v6W3@8!o3&x#de<i6sd4$kRj-$6&-<17f(0Y%9}o$v-b3K8fmx#}$GJj$%xbnZ!}!1L~^*=Z$|S$0(6xRvDk"
    "ygJGurf&wMf)(!-LKWOx?}cIDUTTT-xyn63*1o7QWV;`YZ4CTiwWX7GN0l6A<rK-p;N3w{h1y4>qZ(@v(<%%dLH&c7X+9&Qos&&9"
    "rD@EuUD2d-n_y1VJFkfMo&0?ZG1%-#E(*MaRUdrXIrR&R0yM`<zHIq;NduI*hMdJ#LIXBMT2C<ZZveDg=4Be$2`67GLBu?-tWxMc"
    "q^jv@4|5ETz5z<t`a|aNV|>;^w(({7jzNvkS~R0Utij(1HG#qta<<eDe#%fRKlMfQM5RRbKVZ~v@-_?E0Lmt->D{6gZr2BrL0ypZ"
    "Phz0Tktl99R~c5jJp*2U`&D8sDklgS=fGsHshkXG4MB@$t@HH87b6pG)kkDd%7w50wnKJKa}?AoGNGDd*&O+Gs^idN{w$n8g}iAJ"
    "l!kIE;gYCEGSHWub?X4IF(CzV!%0@XM~ny>8T;5wI3pt`+Pp3WSG23I43q9wq`nwTrJt=%l=sVQ=Al15f41>5^?R(YLp3Bcsx#Xr"
    "A8acz#A_+yz3=vV^d0j<B@*6DJe8SA%CT4l%qJTu7Gq7*T0i^ShXq6*hC@HMY<-|4eV~+Xy@wdyjBo=NPhIHqz7k{6A)28en!Gwl"
    "M@2AoR^Ill<B6lsAp389POcGCv8<Z8v;n}kE%cRC)sn-~$1A8&1ztPBp@-HN=kEZ?R%#G|t1hAB9uhFsv-_TQ5)K~|xh}+B$_-3K"
    "0U+r)UArge9l`3&hAw)ElNmVh+G}EUHG=jOrB}}zFaH>!t|8M%U|Pt1X38%Vk)#t-1+ylsl<@>ndPXX*!^FZUDU%gCb%Yog&zUL{"
    "Qp#gIE6fd>mH%q*DN;QDZ>+`EnQ{@Hl_I5~Ik0W8%_Fqjc;=_G*p0Ub9EW{NV0ps3eG{-W9q1blsKOX)rJaS!5fvpuoT1Lnwe;cx"
    "-Jw7SAXjG~PK?BNt}|Oa)Gp1cZu+i$(&LVA_ZjLf&s=N{pG9^7DRF22bohF67ts$p0IX)hwuI*fF-uu|<2*vD4N4PQXr+>+32n&G"
    "UJVo@c@xr}!dL2p#1D7AqcYKcx>Gl&7hj$(WMVxwHbFr<PvB(G5xCbf6*2q7q9zsV32Beoa$El8>m6|E*KoP1k+4U1#{@0Y5E)#!"
    "z`dCzwLIxTa_<2UlydQ+HbB*D9Cd40e#=1K;SKKQ=PaIxz*b9o3{0t3))zetLb1Dg!Gs?%dHh#o!5Fgxwfe8WbAue-5KSj|W)I+^"
    "k2e=7upjLBVgl{W&3=ikiSwM&gLT`;@_o{A;FU78=Qe<n-+?Zm^;24W<^h8eqX7N`YsQE~e!SUulDBYl!6S81hvS<s_KEf8&muoN"
    "1AcbEuU86_a1=txD3}Q@obHhWNzi(9?czWL#m4@rxGTPQ({V<T-n!-jC;hKeWzc9rNW-~VYJQmKt*G(3Wv;zvE^`~J1Y3;}K@&^R"
    "{(LJTn0zN_pbj}{he<=JiicOu(v111S;B#)8NED~x0q!vQEZd6Q5StGwanc|?x%I7fcD*v!#QaP471C435N+gT8X&7Sl;Ni5^CtZ"
    "&a|C#EyC~iud_^w9Q{##uwaDxY%!TujdRmD$EwS>)6ADsf;D$VC6m9~{P^_z$_ZKVPD?ya8XHlV_8AoTbgLXm+UQh|A!3s4X{hD@"
    "FTG)I8RL5m$^8=z5eYs)UdcVg=(?=zUqMX`Gmdabm!K-$5N8ZFYWY$|hTRY9yWmS?0}LnctLYR!n{Zv-iK+D2wtsNx^U46jcZsPg"
    "w&VB<42FA_(lBPE>dPHDqVl->jtKq6@Gp&Gv!{hu8$?snMiuNY1+$Itl}Jqe&@AI6s`w|1m6IGA)rLK-l@PuB`=gvZmqOwnJD>wa"
    "Lh(;TZQR=jgSy0yyuSN%FbbFYTa>^o+HZS`#CySe)FRNy<7#Clbfy8XCbWjKf$m~+YLiwnASEfi_OXW-=6V=>L%YCl-2MY;i_o0X"
    "&CE|nd%S+&&2t}$JM0~i0&I8?#(qX}w~%6|A{s%JuhRH+dibz-^(;<G4#wHXLe;9c11uf{M3Fd5W-mt{MRjPnGL(QnRQ$6a-muf{"
    "if(HrRrvF-4K<whP|}={NttV#r(?Q=WGA;B_l0Ov>P_OP5GdP(r#zbh364LK^@0t6WpdHZo3Du;MtSoyQ4%HC-T!q0-ek0H`ul%D"
    "i?IjXoxL1~0DV^Qs*fJPrFd;XF=aXFWhtFok?DiyMdPr5U9diKQVo9Q*}3Wtf`C+_u)*<zjB0E;9(NrU5!Nx6!4OdVC%a+#n%E^`"
    "ZwQk}3;!QOeBX<qL2_e<jhrCDs9bMHsHKce9Fj2=wVoe!GM{|L?wv$QM@VEz-MaA+R~OZ~Ftjzmf0f2sj6!DGU=Wi^32t$Vv*Jyb"
    "m*mUJ%la=vblF}mL$p-y7@3Tk!$xx{7T-LAg1MI>W=|8guwasNLkvlxxk4*;Sf%Zn41mq1q}bSHWeqd{nuqnv_%dFp!BS`WO6q7C"
    "w*M;L^!3gqOPZ>No1U&izQ+_0F;M6wIK-ZI(#K5`7RK7Xk$d~0vUl~vpzT?*y!S+-L~s1Bfj3#LATL%!ad}Hm?W>);iyV~gD1ls5"
    "rSOUKO`6+hmyqRWR%iv-$}=dGt>+8EhTUgdTMD?i2Ry#048o+oos-{d0{5!aZWe9<$;kbj$-x(=%9SF!Htj^$c-7E*vm%U5kB&=u"
    "RX9MkK@++A>oXUwC6vIYD%;nUBUT`S7Gz#l`c_B>Uxxj~K{d!qDK&Otrdu_df0pRHx5Ns?{p3WG6>!s}x~Fr??ofzb%hGyw*NlS8"
    "9Z^D4-oqG^545!)5#vvcQhT7+OcC|9Y2aqLv<G#fK!`Rf9;z{w*rmeU({!Hw!-RfYS%0<N>8m@eN3T9EO}ohfqX{x2Cmy^zdcM{n"
    "Yz-z)u^<s+wn|BOWs;XODswh@fYoBLth>dK_2lL4`mm(Ox_l_^-ID2h{JJC7jApIvXm=oJafgCS`2r$|3fDk%`NBqU(s09hA-79E"
    "9Xo8A(<o~fsk5NSd*3z6?DNQE<vdH|(CBmqF$$&6taqZkI|HKKgs(86(KjlQyuUh~0Ji-w*Q$ih4NwIVeg3r;i8%e&@82!G>P@^H"
    "NG=7FUDa;Qob(-`ICDPveEu;>&svNV2&@uN!r4xsLwC`MQ}$4aC2P^Wy~cSMMYcsjj6Z1m0y6hYlCztZTs>rLEH4z*j>U_}X>Blv"
    "F1lrN@&>RZHk19U@kdTIS9>6j+-5MV%ad`V7;d@4Md$e^a*KaX*BNZz@QSkBREXZN#D^Gi7yys`m*k01-yaBa1@he=9XLW`ym165"
    "$V~OZHtwYHRmDIOQCz?Rk;O^3k*(t5s12_xp;(a_vZliu!lJ4cHWr;R&4<kLDPkNPgtfu@3$B8$W~q5k_UAvY!HIP`KLi|W0p!fv"
    "W0ax8QZ&>kqy%^%^$L_RQa{Xb4l2kZ1|$_F1wtP&;(BktxdXV)nQrjzyJ5HmcZHl|M|qy%M1@L>|4UqK-=PD?)%MuEPBB8Z+8`b#"
    "a`xQ}KeYN^rFf1J*?pN_;$I0hNiFAmzy8!w98y`+WGqWWsw`I_QCdgGP(?TrPWW2u2OM;9zyURTaIl}EGJJnLvE=2_>g@)zB0K)6"
    "Em4(t@6WvGj?+O!$-lxy)u4`uu>VWBHkfnGy+(QZc$l-O7kPj=;IAa02z(fK5A%YG{O-nBZl*Zvmx=Ub%M+c%u>a{KSt>5|J3Fxh"
    "Zt{GS4BGnABZR@DgFj-0FH}~VNPPn=pOtpSUGt4*+3C(;e^}swYo_{nLaRVr6|sbAIF3VYTi#fb`o^6Yh^Db0%gNBx5hcJp3?5nw"
    "8$a09QwEPkToWFy&}9c87a&C-EL6u(7jUstJUka3dK6h$9W-Ra*8{_lE2*b3_pbeqU&|aI7yTB4_zA`T#q0aA#2g&avPOvLQtZfY"
    "wtCX1OJE9y`(nL|i_~N(Fvti2T%qm(wQwUVif<VJgmAGO>!a|^78YXsK>1Iie9?|GB&z{nHv{zjbeF;cuc<b+3PK0hDMp>Y_m3%{"
    "R(B)xCBJ17Tw*ulF$7~LtTzH7s~fk?T{Tk`#OS8!p8B*;EId$<zG4h~Pk5J#epb%yEF*BQbQ~0*Hu(+8+8`$OREld-Z|jtWvpy20"
    "hRk~TZbb<KmU?QNDDMud`{bc7DVi3(3d4?qGZdKOd;(Z;4&$2}bd1Ff*xCt2>-7jL->juhWGW1O2kLlO*xEOrKuD4C{+D)Dg8BtW"
    "WC-QS^QQsq?*N0-?kw->?rQMrbzylhe1RbCw?Y6M-ofo&`GvCKbVo7qgzbG`=6xJU>H&#=E0fG6IlyMtf&fVfF(M`hvawBc@TPM|"
    "{uVe5dQXWt8u-IRt3s9={a{Yxh?NB~cHo)_EAWD}{iNg(N|Y;LRsg)Cq`w1B9s)a5`q5NBIo6?egv<UQ@U&60w(wM!oxk<RIEU66"
    "S5@x=&ZPi+lfb7j84M~%rOCfkLw7TnCfpr8#9xO|ChYB4rOD_E-6OF07?#;l(;(!AI+j?Ra{TG44|%kd(2!hdQ2U_)&9G1P^@5Qv"
    "yv(pebXs8qjk>B{^sB&P-;D6(K3?u~FhJZTtpz2HsJ5gF>IlVpZ5N14V}}jp&Q4gWFw-=quJC#HR`|D7SH{ZePN7INf)^t%l+};t"
    "8$7CaDrcNB<;F#56Z#(KAPWEGF98$^tSx7r5-0-?iqPhA1Gv~UmojIlAwO!wPDAX1A_|A{d{<bE{FV~cJzrsu#b%Li7^x(9XzKb9"
    "o3>G0BpLJmAzNBhX`!>aGN@bHeWl68r>J;atrt-UMFu&5#n^Stxip^YVtzqhKV&E2>vj1*;$o)3@ONUIl!7&lJ>w?Q5mxLTW~`FH"
    "<EjaPh6T9gdXetl@c%CK?P*K-w~PAaKJM6`+-UAyK;!bj1q8k@-PF*n+QGpfO={k5?Y@i*!nb|}o0B?_SFwiWKg$K>b>iP}%oS-W"
    "NH+0W+p!CZC0C|tR#*l<7`7EXBgqzP(a%=Itf%XPe3jMbZXH+m`^2Wt@dNe&{g-^p2i>YG#;;w=m#Y-+4*i@O$Oq{e#0;&reCeYs"
    "<L-m!PLf{=AIxLW48*t~w3&nRb!<sni;(W&_=SG6q5uC`i+$pEiP*ufS6>VLE$!U<r`-lSiWsr@r?y-*F#<EEYaasZKbIY%`1rkr"
    "WiNc@m?VMU9yP~61r5p+)6w%B@vc7{UD*tGn;$q9epnpzQ?#a3G$vAVJdxJ32%d2&YX%Oac=%am*Gm81!eZsX7FC77mlWsSj8#I;"
    "Q5T~hxfhp9VMi^=wlTWeTgclm4FmBd=V*=-Q5lGx@fsbsC)^z!nfv&m5n9QH05Nh+gRWJ=8cJW`zaeD&uoUp#ORi_<5&Xxbyxo?T"
    "biF<0c1;3M^r<IpD<i_iMyJQKlYtlXQ$!fkIX#F#zyx6S1c%{z>+nLrl1?4oX(cWHe2hJ~fDGoacqu3O{pbF?(L%RwsdHXxuBw1l"
    "B-lxudxC>mXvIsKf`G_0KPlEJH3c880ABV^Z^t?t^)OSKe>-XVx|Pk4g)A9}`XZ}JpA9{e86p}Hrrxa3olBk;9C7atM#(H*xP{7y"
    "U0Ev#kXZQ^0MgCbK8fvd+K)h0%^hCRhoFo_ei|1LX~{=D8MVy}<KB1J1092OdSQKurO4iXLX|0-5lDGitH6zZL17>55tYMHINeql"
    "mX!Cevy?xodeJ<7Dkv7?$2TaZt`8MM@=HW6+w1RMK`p~!g#DNsKM7DUK`veSbx)it_TxtWr)Fj`T`eiI)$~1|!bAfo!?x5cOcb)N"
    "8=)eus}vMH{S=urVjF_^%qZa@k|5JF<7UoB0YXS9;7U7s;!$8#vPhVe5{~=&NzEXl#rubnPZ(MhhJkx<jcErt=fS~g;j|*X1pxH+"
    "n5&n0-;dBm_T^a6#8kFgwdsztO=R);n?H_c)&fU4)U};{51uKiG}HCbeo7XiPe}fL`%XJ(Dh-xni5*=!B>0$e*<RH*e0xZ=-2{pt"
    "x7M0=R$n1?H40Y+W)S%^eHGM)ZzGigk%g-=gJR^uBhl#>k^k(2OyLu<Q09033?B!6OuG3o2u%4$IQ=dR<;|vqyNP$j4n9}6*vuf%"
    ";MQBDT)CF3ksS8&tJP~2=W;%W(jp@()k0vp7p_R3p}>YdOVf<w&q7aIh@AkJz+<1<X->P6jxo}t%R0&N!JtYG_qhL{M+bLrbVU!m"
    "PPi?9P}C$INn+bEy<4}E7qqomib|a-beKJCyUqNluTJyb?Zw<&6ovnA{=fuCV`3nWkI4@3NrM|m+Jq`vkT=-%6+=_1c6=+QK_<y|"
    "h@(|H3?Pnz$<mMESqhY7ES?8zdG-D6!uJLa_^P$=B5JF5yl(O~+Y2&Clb_m_|6d?JC7k9<1U#?Ed0pB%oAZgR;Izd*Wcxd-4mj^l"
    "+gg(iimTW_?$YH>lxlzUxEkjZN#_%GKQq=dH<b7{nBP>SrBk6k80LWKfrLGJqZSKa>)YkXCbhBz*l`s;%UHL?LX>!oq*+bCaR`t4"
    "g~i|kj;X@vc@j+n@jifV=l$8J<>V4-4yN(cm+}&Qn=)&hrEqheckC`50Nd}aI5brMc`x(m;9anGA^`~W&6}OIGFe=1vKiI2edp-h"
    ">~&IWqPmNTNGI+HadzP#uAlE0)BVtDV6HaH2LCTZ_x6{w_#?VKEeK1(k_|yULK7_lfcYa@&N;_`;&ichK=1k^<ZSto@jw3o|8S;*"
    "!JI-^-ca&KGR=bn!(ilOzf*(S28m7F)yo#6!~w>jHHPZ&cBiT7=<Gdm-M!BaIn2%^U*%i>o^H`&e98A`O;2g^7p;g?o;GTEnRd~("
    "guQSq5?{vZXwrFOPkYhd@UiC6u-0aFs_M?V6;w}&1Kyp{M;k@So;#Pwbfasim@`J8n$SbwRgC<>UTXd#zQP#|Kh*uGWI03#2>cv}"
    "cqRv$-x8SqlM4-n9nHU=9)zEo3lY2h!UVE#<;|^D74wcQVQcDh0!$T{UQ&O;X6B7`L(4g{$snpZ>;*R}IVS9{tbGygE#N=O(vqk|"
    "NJuQM1-ytlLlVq0Q3Rn5TTe~${FJ#YFmLoeJg?8Lv`m3u7C}s}dkl;!1QP@BazI7oZ9jJXYY78dM$6wu)ac92P`qdJp{tE)yO0wq"
    "ind?bDq>a=8~Hh*AeMu2vNe40xRPF7aI%;~?5I}-qzDIjf5b*=qc72RMb3FkfC^V5U}E*sB2FQ8k|d~q*_Ww|vwj)hmB#C*5r3hB"
    "sxu%dZa$HPjgD3BQXud6CXn9!v@~HBN=H1bLOpAi3Y*YNribGhJhv)DXNj2i1F~rMMDA{koD?a?KGiAp`S&PABcOJ_jYfORyPe~{"
    "vv|g%z}=U&7CqxD2KJcwu1q5%K~MVfsnZ*q$3URQ#xBs|P6c&zZR=7}tQd4JTrjJ?vp^`wO_K&ATKPZBo{w10w&u3EG0&J}1q{8I"
    "K48%6vW$T3_a-uDhsPC<S4*^>abIuMqiCfV2+3rXFN5qxkj}{DQ8Q<>+$`pYuBcvXk_Z+AlwD%l(20ycd2n$zQ53^4QUplZXaP`N"
    "!k&s|h?@rNg+)=}#T7sdeZoQP?G9s(eY{C&T=$O3``(#}m&d%t{A7#YyZ`YQ1G1(pc!%mdaxTO}t2~G-z+6yLB2tC}%bQjYJwW}f"
    "waF%^QN7NSH9rHK#&6Ee1H_rhEUHnun+IJ2MD94yg0dLFDofS`!K0EmRX@N#%@QAvONueZ;-Jtf!!6ZnqMHr*0-~*m6|h#{FNo)A"
    ";&e9yW0p5n0SK(?D+qF*lhhD2YpA!3iV48PoxI7=tMti1-x2>?0Ip`Kd%{hA>(BMuTY6zJ9<bw+3BE;+aY7MfJBu)a&Pn>`(Um!s"
    "kT4y)^)Wq~SBS-?9itikISk#yJJ<U6QalB{Q=K;=*D7QA(bWnnlH97)mJ?8RE5zw)MfyKwIC<f_Wk(ra#9&Lv28?&9Hs?dcE_RU#"
    "5@oe(g6D_)`}T*cZTAdApWr{FG`0Q-EBT>3z@y58_m0*{WANd8^M+Hs@q*gRhDLIMhDzzy*tA-Q#UGjAjZx<j4C?v)T^kbPOh1#y"
    "QVqzntH??jd`S5R5#Ap#Uq@a3hgS4Z3=17`FdF*GWK6~kb?@v=926VWOkM;lui>w5Q@~#BorxO--6vrg@E+CkTcq4xfN>=dam0eT"
    "mGh_B+Yk}U4p>1`af&o&Mh4Hok9d%cma}`cMLwDY3w3P0dks3ErgRCruLtMJgx;(Nxv4HUuQr!PY2;X+tI724rPP!Qw7aBnQ2JGe"
    "@wNTZ`ib8p8w~gn7;95wrBA+Z;nYSyUfv%(K_jHc=b%0_742ZUf*J@&15Qvo8tV1m!l|;>YjekFCF-BWG;I-C)sJCB99m2Yzj2^H"
    "0^%7?*AKtUUlYIV9<HcfUPU$JAK@?#hB3}S8@|V>OIc~ZsR@T}@3VQzN7u8|&lSEv*AFx7jAo+`Soc{E*C^4mx<GSt2_e6aICPrZ"
    "zQki6lbjs-j(oz|O2Mku`?BXXu=dfsL7TGAa0sAeaDf|ViGKuCl`=t^-7un>Qz;hpuPf|jx)*WTJ%f?fnI`4?;)*6(xekY}syt0B"
    "F+k%9CPhhF>T*3H7+ksPDZ0agaTPw_c6wz55(kkGlH-nc@<=Poe`^+cSp(6(t*+g~zFP>Ohk0w(rO4F-K7z>#js2mr@kps&J|5z0"
    ">87tgw~3t=xp3K*HOi<?pgEt@-Ff#ujS|{f;8%mrB+S!Spnpxq3h@iO4@{WK{LjR&36h(U{UGX&j>N1wuC!aeNeBtTT~=MrSH)QX"
    "2mN1k73Rpy<eD#i8BVG9X`Jnt6$ege3#h(9<t6}aT$Zf)v>T_Tv}K@McFsjK&|bb&ju9u6F;Ju&VvpwSGH(FouS!PR>rC9(@OkXP"
    "=A6ncKU`Bbc!N@__v_;Ae1Pglm2Mwv2|R@7d6~YXJ&0mU6Uda#7U8+f%~o#(Gj%-H7`vEW)XiK-WIC2N0tA?jV|cjvU4&QmUq}*Y"
    "ij3*9KY?>BJk7_-j5Mk^!9+-PpU=vTg0udEFapBTU%<irJM(rNIg)%OuSb)jD6~ThPuvYc7;_~s0zgkn#!spbB(b8G<>ox2jKBq4"
    "(EgXx)hN6mbmph2YAA`G4#%(ocZ(7WsK@JT5q3^urEy=WN%*zF+Q;7!`DsPe1fKcM6xQ5CX=SDT0Uj6eBor_f_w-VGP#5{mO(ufp"
    "Qhexc|L%KhUDtZ_QZq%4+aPV0;G5(M=SSn-8byN}8MRYX)TfWa)C=J6+*EnTb)XKWD=ei_+@&B8#3+`_qtB%Lur`5xqY#s$M-a@5"
    "g;7=|+$C{oyh67Ai$AZnu}o}BSkA~08GqYn(F&IU(!}>~C=Mj~%u`#}i$`er9Nq74a3;~JUn<aX(;vfZmrSA4HF8XY%UMz8d>6eq"
    ">M+>V)r0Y-@lsDEWIR!vHCdtl;v|^n+J^b9>Otid98Rw9u6K?8t5?*sUj~dPr&X5kix7QN<NRdSq`}i2G(3!z%!!-~ct+vn(~IeP"
    ";q(tC#BBhu0!=AZ11*V0c0g=@d3xQk0f3Fv3=d1IKS6>8(jB!PK!d^}-CEWLe5Z-zA{OPKga~QpKilt29ajap&vzo(|MyhKdSmwM"
    "aFNF7$nVG=RmAH21&$e_^7K*!hrb97uV>e**jh=_Bqdmg_M0@ffo=MYUi)!EPsEf`KvP)^ZWcl$sr84oyaL~JRB}n+siL+1ARthG"
    "eE{>t&tRFc3B5d-D{>!|N*w#>M5h><)B<;QD!r4TY$0X+K|5wn_+e!_JN?U<&lXyIQ0nEhPul_IIJu4mao8IxJx3fLu`X4T`qoR>"
    ";8*;@l4^wObKJkAXj`%JuCcCEuw^5KgC>-k{u47kmu&H(MYM%XE`6-9*qUxDHg)_3Urp$h+Nj17b!lehDi-WsGC%wKc6;2-yQjgP"
    "7+#Nx&I=)_q{f<g#{id8o0@Hk*q6B-0gh6zk=dYkM@{WMo+u_KtvR`Xn0=qZ*&=u*puqwAlEk<N*fUgFSee{F8KLeYCRyJN4ov4^"
    "h^WzJ(^Lk<heV6qS%M-z5NXE<-)w?Z1OQkC&!jbiy?52^V=G|3aw8DOgk06okdj=X6L2<g$Cbq$$)BgWxS0BXYz&?%9YwKOKwKP|"
    "aN#NlbD^cf0L3ZYK1so9(S7zT0Q-zlnSG1VA%yJgcHp=dTQyY_AZ)P9{)V;l5%y{HTEw3tvr5CaP*Q_}K<x&ai3uxzk8|>QZs_=I"
    "1uo>TNPq6n!n9+pvCJ*zqfh*C#sY-C(y=O@q$z7;s^S?ZQA)LZ23x=8*liwJ$1^<C0Y3`K?8C2Ol*pl=nRGEIe%k9;5UY0-@11>2"
    "B4tGT7zZ-FR<hL_JA$Ilt1&!K!E(+Pxz)|T3>!cT34)dbS?Cxp_S)S#()I<B;qHD$oYduVREKQmb)UzF*+tECcJxL0?qjQb6Y6<6"
    "!#BqVoTgauxY(dMJ~(y09e>M%*Tv#B`xht~zULmz=t`C|l}*^=)&(d8@q7e7e--h_{w3Sm6-tm#iNV%4+W}F{0D6c~Gml_Utiw4w"
    "Vf{&4RMeog@IJE~XS5$kDBAH(Gab|b=iQaPCcd>L+_YtU?q|ZgD2AM!%)p_Zzn%|>VkbplZRgcXlqV4c%X1sTRmP$*LyC#_Y7|K@"
    "eqc$rUQXBDgmOv^wCX_#$MvU7o0`)b&bUe`#g27`SB(`sx5eDzyZn$uqi$Jmn~B`1{>qkneg!*iraZkJb(!<^)Y?uep3ofNxLHQw"
    "sax)|O!gYt#gHao`gx~=vH?$ARoZ2~DtoKiGf{%ckNL(Y6^P`fodehOag2pbvwN>@jiZ|kL;Oy;W(Bkxt!F4SBN;`;>tRd9OKTg8"
    "nfiU#>fZq}-2f~gi-fB|76-Zb6@bYOQ7~!O#v|fzZdjPyA%-XL)@7lD(o&4(CG1D=5945C3-Ax?)oTh3oj75NIARq{s0Co^TN;nR"
    "6L{ze!a<z<GI@@KepYxexP07MRZhgIB<Qrw*DA=oJ}1uJjbqmBk&uDHP(UYw?gQs6g6KQ2O#f0VfhQEwgqC7sZ;-Cb=3vHW#$ea0"
    "O3NUTh)?KMvBs!A#moqc@!L$a!qEiaL!!qh&Q%ERbLqgb*AWoTx591bsS=diZx4i#s{K2?ugAf$w`(5kQtW)tnEQ(NNQUgz$4IN+"
    "rhu^K6k_b95~mtAHum@hq+p-COHkzdZjq2csuWGTV8LgFgdox{h2;Gz>aheQ3gouymHI_oTptrIU;oB6ua#S0!(`o^?#PLGt_}66"
    "jtfmJb$v~4QHOn;<(O=6ol(6AQE0OEjswHB?kSTC30N#gcaR@f082f-{M>d{DWtx%1)_u-gsfU!{M;~>aKS_r18YRnAz)oDQ`@d6"
    "Bm{3OcjYVgl`g81pT}Ek=hR<Pn2pXjPr_CeI-w;{v~%_yvKWPBD?29%JVPOVQ(%7+gbS=z1*ywo8ycBLSF$%^?uL3s<-apM+vy??"
    ";gGecEF67T5cf`W0h~*JJ<KwBx~rA<sgZ#pwgIV#QzWSF1D<G4j)DkDEHZxW@$VxCvMj-<7pCOQW`lTMbjfMjTTteeQx_FcHFMDb"
    "$1ZNoGxQgBO1>DUhTUFaP~mdzu|!eg&ccz6Ue%Tr)aw!a5&RwML){D9x7xa%Ug~2(Oe-BRz@JJkmXGZ-^<&Txsh-xNk?KuQlNF^K"
    "Bmdk#fygT6<x<Xya<DxYGDUxY1i2Uy%1_jO83-@<{v=^qTD<?au_Oq#>2}}0y|AX>b6{I?05B0f>|v7Eo9jvDVfSp2(xQtkLxCk@"
    "2ESVcI45+om4_%RS9`R%3*^FZI-T9>OvUjVn<e>+Y}{V*u|e>xQ6*<D2(LdxvV3bWxwSS1$VPun0kUttLA(Nj4vrwC`)6m&YGLEJ"
    "9neWE9pC~?b9I1EE`NMIb+>ap@vK3askTa}6QC&etW1~Gnfm|z3+W_rgNKR&iSx|W*Q7t=^hSo<io~Lz0}mtDHx#3{&y@N>D#KEi"
    "7dq36RWR%iG9GtPs8^Q;i+SARx3jF~={b8)Av=RXa0PGOUlWtn%9!lzf*zyh#Ft@-#$Z~{&p;jMsNSv$$|4-c8Et=|38=G@ST$hJ"
    "d4EGlOuvrX#SCo6-v@tYghIu4j^dWi+!&QXkcNh{&e{(A173P345CEBGWi&D4QR2_%eNzmKl-Z#k$hJIB)(ka3p7X-jQZ<zMYMYX"
    "Kzyz`Tx!vS>T;SeKM(J;V3``k*3q`#TjIzT2N-bq;Y%4>t~wbhdq5<9;eW(21X>5ElExnMAgo9vMW{ZShY7qr?O7aqb>~2n3wzYX"
    "<@3;;5P+5NI6&|GoIiy(5cqYJJjMkV)?IE(4-CPDN332|0jF`D=-+A}g&ZMS;AifqmCytJ(11}fdS*iZ_-WjGfN@&WzJp5Ddp`jq"
    "{_c~|z}nRB7rz~Qw}DOR3YD<?f{pp?r8gm-lk(%bYS_|31i84;#86oNNQc{)&9zEZdEh>!?C(2IlHs520hGO5Lmu%AtS-+$N^G|I"
    "DRpXeLVa4@BhxeWyX8lnb*qrA#uT?!*~%yy)P29;as#}I$0$ju5c;p_FtgW8-r<gWoU@Z^UU#w~>&@#+mW)mLS_FYGFxnpuz};O2"
    "jn~jT7w;hWa{s4E($VK@&hmzlV%wMM?{!)!hVGGIiy213;!KGvmq5L9@J_2;J-Hr`hLzlRe3DgKWp`X+iINaPIBU{^3ohvPl>|<4"
    "_fKnzQ2(hl;6xh~qAofkuh|@8&YpURh6d>CZV+x-f!UI_e{pXAeshf|JWd|B(v}9pJy?{>bD^`%10G=f9CD!{q&3Synt|YqnG^(v"
    "uWUZ7TV2mhb5Z%F;-oHNG*?1b^yqKN<hz}vTgV_!&_iH@>(R)SOSb`c*%D-)r{c*sRUzrbodQ_e8)%~$TG3;VQ0+FYUx|^(<-Ii7"
    "U@pGx)cfU}Y(^x25|>il{AC0iyJ@jc-FpwT8sT+~?*<wRB1R{~gsqsM!CtZwEWw}R=$0X(f*rPvLnkw{`NGJE)JtTE2MLEr(K$DQ"
    "`itm!9juy*WVu}-3}c_O0c*uuKbwz#;U(BS_8h#*1<ev$m*09Lm38#1jwiIqI39k?jfCiCF613~I{4x9Z}%Js7*a-hs_4r6a)B#)"
    "FfY<t?yY|+<0n~7x+I9<%5po=O8~+`e(QT2j;0OX0KYeaS#>8y?1qG1F>{r&47D=byTo!wGogdar!7W1W;t7&UMabUusZ+e%4@=R"
    "&3!|IwAkA^`x{x4{bbw#c~a?16ccxBMo!zxG0ZB@-^Js+miwSWNC`vXi-&Zmm1BegD|!qA#248u>#=_eabif`(%2;!U6Al)8Wrd="
    "U{$^)*MswDh$BkJ09{>6HCsyFSdagqt2gDj3bhPb$NVLcuV2+NWf4nk<E0lvv>}oGBO`9gV0(@B4`_g3U+y6={aU!XJ#U{i(9R(o"
    "r=em|;L5LR*nWO=4w6;25?@T!SnYnDI7y(}K<kRduQ;qb++5d}3gIP6Ba^h-S|k@3-LX-cRGv+q(&Gv0^YBoGaW5hLt_|tG{TL)K"
    "$-KoUP$#esk}8^K7&f`XAvJ<rc-Nc6C0MkP;jY9I^qwhD^cTP|T2<;EQ?{?ka`Jc1*d?IZSEC#CX8Q^N00000-@Muk)(@>i00Hj3"
    "0jlW&(DbbevBYQl0ssI200dcD"
)


class ProbeError(RuntimeError):
    """A sanitized probe failure."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


@dataclass(frozen=True)
class TargetConfig:
    base_url: str
    key: str


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    code: str
    details: dict[str, object]


Transport = Callable[..., HttpResponse]


def http_request(
    target: TargetConfig,
    method: str,
    path: str,
    *,
    body: bytes | None = None,
    content_type: str | None = None,
    timeout: int = 90,
) -> HttpResponse:
    if (
        target.base_url != NEW_API_BASE_URL
        or not isinstance(target.key, str)
        or not target.key
        or method not in {"GET", "POST"}
        or path not in ALLOWED_PATHS
    ):
        raise ProbeError("route")
    if body is not None and (not isinstance(body, bytes) or len(body) > MAX_REQUEST_BYTES):
        raise ProbeError("relay")
    headers = {
        "Authorization": f"Bearer {target.key}",
        "Accept": "application/json, text/event-stream, image/*, audio/*",
    }
    if content_type is not None:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(
        f"{NEW_API_BASE_URL}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise ProbeError("relay")
            response_headers = {
                str(key).lower(): str(value) for key, value in response.headers.items()
            }
            return HttpResponse(int(response.status), response_headers, payload)
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read(MAX_JSON_BYTES + 1)
        except OSError:
            payload = b""
        if len(payload) > MAX_JSON_BYTES:
            payload = b""
        return HttpResponse(
            int(exc.code),
            {str(key).lower(): str(value) for key, value in exc.headers.items()},
            payload,
        )
    except TimeoutError as exc:
        raise ProbeError("timeout") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ProbeError("upstream") from exc


def require_success(response: HttpResponse) -> HttpResponse:
    if 200 <= response.status < 300:
        return response
    code = {
        401: "auth",
        403: "model_scope",
        404: "route",
        408: "timeout",
        429: "upstream",
    }.get(response.status, "upstream" if response.status >= 500 else "relay")
    raise ProbeError(code)


def decode_json(response: HttpResponse) -> dict[str, object]:
    require_success(response)
    if len(response.body) > MAX_JSON_BYTES:
        raise ProbeError("relay")
    try:
        value = json.loads(response.body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProbeError("relay") from exc
    if not isinstance(value, dict):
        raise ProbeError("relay")
    return value


def parse_sse(response: HttpResponse) -> list[tuple[str, dict[str, object] | str]]:
    require_success(response)
    content_type = response.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "text/event-stream":
        raise ProbeError("relay")
    if len(response.body) > MAX_RESPONSE_BYTES:
        raise ProbeError("relay")
    try:
        text = response.body.decode("utf-8")
    except UnicodeError as exc:
        raise ProbeError("relay") from exc
    events: list[tuple[str, dict[str, object] | str]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        if not block.strip():
            continue
        event = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
            elif line and not line.startswith(":"):
                raise ProbeError("relay")
        if not data_lines:
            raise ProbeError("relay")
        data = "\n".join(data_lines)
        if data == "[DONE]":
            events.append((event, data))
            continue
        try:
            value = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ProbeError("relay") from exc
        if not isinstance(value, dict):
            raise ProbeError("relay")
        events.append((event, value))
    return events


def _json_body(value: dict[str, object]) -> bytes:
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(body) > MAX_REQUEST_BYTES:
        raise ProbeError("relay")
    return body


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def make_test_png(rgb: tuple[int, int, int] = (0, 0, 255)) -> bytes:
    if (
        not isinstance(rgb, tuple)
        or len(rgb) != 3
        or any(isinstance(item, bool) or not isinstance(item, int) or item < 0 or item > 255 for item in rgb)
    ):
        raise ProbeError("invalid_media")
    signature = b"\x89PNG\r\n\x1a\n"
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = b"\x00" + bytes(rgb)
    return (
        signature
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(pixels, level=9))
        + _png_chunk(b"IEND", b"")
    )


def _validate_png(image: bytes) -> None:
    if not isinstance(image, bytes) or len(image) < 57 or len(image) > MAX_RESPONSE_BYTES:
        raise ProbeError("invalid_media")
    if not image.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ProbeError("invalid_media")
    offset = 8
    width = height = None
    compressed = bytearray()
    saw_end = False
    while offset < len(image):
        if offset + 12 > len(image):
            raise ProbeError("invalid_media")
        size = struct.unpack(">I", image[offset : offset + 4])[0]
        kind = image[offset + 4 : offset + 8]
        end = offset + 12 + size
        if size > MAX_RESPONSE_BYTES or end > len(image):
            raise ProbeError("invalid_media")
        payload = image[offset + 8 : offset + 8 + size]
        crc = struct.unpack(">I", image[offset + 8 + size : end])[0]
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != crc:
            raise ProbeError("invalid_media")
        if kind == b"IHDR":
            if len(payload) != 13 or width is not None:
                raise ProbeError("invalid_media")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            if (
                width < 1
                or height < 1
                or width * height > 16_777_216
                or depth != 8
                or color != 2
                or compression != 0
                or filtering != 0
                or interlace != 0
            ):
                raise ProbeError("invalid_media")
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            if payload or end != len(image):
                raise ProbeError("invalid_media")
            saw_end = True
        offset = end
    if width is None or height is None or not compressed or not saw_end:
        raise ProbeError("invalid_media")
    try:
        pixels = zlib.decompress(bytes(compressed))
    except zlib.error as exc:
        raise ProbeError("invalid_media") from exc
    if len(pixels) != height * (1 + width * 3):
        raise ProbeError("invalid_media")


def encode_multipart(
    *,
    fields: dict[str, str],
    files: dict[str, tuple[str, str, bytes]],
) -> tuple[str, bytes]:
    boundary = "SolisNewApiProbeBoundary7MA4YWxkTrZu0gW"

    def safe(value: str) -> bool:
        return (
            isinstance(value, str)
            and bool(value)
            and len(value) <= 128
            and all(character not in value for character in ('"', "\r", "\n"))
        )

    parts: list[bytes] = []
    for name, value in fields.items():
        if not safe(name) or not isinstance(value, str) or len(value) > 4096:
            raise ProbeError("multipart")
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    for name, (filename, media_type, payload) in files.items():
        if (
            not safe(name)
            or not safe(filename)
            or not safe(media_type)
            or not isinstance(payload, bytes)
        ):
            raise ProbeError("multipart")
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {media_type}\r\n\r\n"
            ).encode("ascii")
            + payload
            + b"\r\n"
        )
    body = b"".join(parts) + f"--{boundary}--\r\n".encode("ascii")
    if len(body) > MAX_REQUEST_BYTES:
        raise ProbeError("multipart")
    return f"multipart/form-data; boundary={boundary}", body


def decode_image_result(
    payload: dict[str, object],
) -> tuple[dict[str, object], bytes]:
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise ProbeError("invalid_media")
    item = data[0]
    if "url" in item or set(item) - {"b64_json", "revised_prompt"}:
        raise ProbeError("invalid_media")
    encoded = item.get("b64_json")
    if not isinstance(encoded, str) or len(encoded) > MAX_RESPONSE_BYTES * 2:
        raise ProbeError("invalid_media")
    try:
        image = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProbeError("invalid_media") from exc
    _validate_png(image)
    return (
        {"media_type": "image/png", "bytes": len(image), "decodable": True},
        image,
    )


def _extract_chat_content(payload: dict[str, object]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ProbeError("relay")
    message = choices[0].get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise ProbeError("relay")
    return " ".join(content.split())


def _failed(name: str, error: ProbeError) -> CheckResult:
    code = error.code if error.code in FAILURE_CODES else "relay"
    return CheckResult(name, "FAIL", code, {})


def check_models(
    target: TargetConfig, transport: Transport = http_request
) -> CheckResult:
    try:
        payload = decode_json(transport(target, "GET", "/v1/models"))
        data = payload.get("data")
        if not isinstance(data, list) or len(data) > 1000:
            raise ProbeError("relay")
        ids = {
            item.get("id")
            for item in data
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        if not REQUIRED_MODEL_IDS.issubset(ids):
            raise ProbeError("model_scope")
        return CheckResult(
            "models", "PASS", SUCCESS_CODES["models"], {"required_present": True}
        )
    except ProbeError as exc:
        return _failed("models", exc)


def check_chat_nonstream(
    target: TargetConfig, transport: Transport = http_request
) -> CheckResult:
    try:
        payload = decode_json(
            transport(
                target,
                "POST",
                "/v1/chat/completions",
                body=_json_body(
                    {
                        "model": CHAT_MODEL,
                        "messages": [
                            {"role": "user", "content": "Reply with one word: OK"}
                        ],
                        "stream": False,
                    }
                ),
                content_type="application/json",
            )
        )
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ProbeError("relay")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise ProbeError("relay")
        return CheckResult(
            "chat_nonstream",
            "PASS",
            SUCCESS_CODES["chat_nonstream"],
            {"content_present": True},
        )
    except ProbeError as exc:
        return _failed("chat_nonstream", exc)


def check_chat_stream(
    target: TargetConfig, transport: Transport = http_request
) -> CheckResult:
    try:
        events = parse_sse(
            transport(
                target,
                "POST",
                "/v1/chat/completions",
                body=_json_body(
                    {
                        "model": CHAT_MODEL,
                        "messages": [
                            {"role": "user", "content": "Reply with one word: OK"}
                        ],
                        "stream": True,
                    }
                ),
                content_type="application/json",
            )
        )
        chunks = 0
        done = False
        for _, data in events:
            if data == "[DONE]":
                done = True
                continue
            choices = data.get("choices") if isinstance(data, dict) else None
            if not isinstance(choices, list) or not choices:
                raise ProbeError("relay")
            delta = choices[0].get("delta") if isinstance(choices[0], dict) else None
            if not isinstance(delta, dict):
                raise ProbeError("relay")
            if isinstance(delta.get("content"), str) and delta["content"]:
                chunks += 1
        if chunks < 1 or not done:
            raise ProbeError("relay")
        return CheckResult(
            "chat_stream",
            "PASS",
            SUCCESS_CODES["chat_stream"],
            {"chunks": chunks, "done": True},
        )
    except ProbeError as exc:
        return _failed("chat_stream", exc)


def check_responses_nonstream(
    target: TargetConfig, transport: Transport = http_request
) -> CheckResult:
    try:
        payload = decode_json(
            transport(
                target,
                "POST",
                "/v1/responses",
                body=_json_body(
                    {"model": CHAT_MODEL, "input": "Reply with one word: OK", "stream": False}
                ),
                content_type="application/json",
            )
        )
        output = payload.get("output")
        if payload.get("status") != "completed" or not isinstance(output, list) or not output:
            raise ProbeError("relay")
        return CheckResult(
            "responses_nonstream",
            "PASS",
            SUCCESS_CODES["responses_nonstream"],
            {"completed": True, "output_count": len(output)},
        )
    except ProbeError as exc:
        return _failed("responses_nonstream", exc)


def check_responses_stream(
    target: TargetConfig, transport: Transport = http_request
) -> CheckResult:
    try:
        events = parse_sse(
            transport(
                target,
                "POST",
                "/v1/responses",
                body=_json_body(
                    {"model": CHAT_MODEL, "input": "Reply with one word: OK", "stream": True}
                ),
                content_type="application/json",
            )
        )
        created = output_seen = completed = done = False
        for event, data in events:
            if data == "[DONE]":
                done = True
                continue
            event_type = data.get("type") if isinstance(data, dict) else None
            if event_type != event:
                raise ProbeError("relay")
            created = created or event_type == "response.created"
            output_seen = output_seen or event_type == "response.output_text.delta"
            completed = completed or event_type == "response.completed"
        if not all((created, output_seen, completed, done)):
            raise ProbeError("relay")
        return CheckResult(
            "responses_stream",
            "PASS",
            SUCCESS_CODES["responses_stream"],
            {
                "created": True,
                "output_seen": True,
                "completed": True,
                "done": True,
            },
        )
    except ProbeError as exc:
        return _failed("responses_stream", exc)


def check_files(
    target: TargetConfig, transport: Transport = http_request
) -> CheckResult:
    try:
        content_type, upload_body = encode_multipart(
            fields={"purpose": "assistants"},
            files={
                "file": (
                    "new-api-probe.txt",
                    "text/plain",
                    b"SYNTHETIC FILE\nMARKER: NEW-API-FILE-OK\n",
                )
            },
        )
        upload = decode_json(
            transport(
                target,
                "POST",
                "/v1/files",
                body=upload_body,
                content_type=content_type,
            )
        )
        file_id = upload.get("id")
        if not isinstance(file_id, str) or not file_id or len(file_id) > 256:
            raise ProbeError("relay")
        chat = decode_json(
            transport(
                target,
                "POST",
                "/v1/chat/completions",
                body=_json_body(
                    {
                        "model": CHAT_MODEL,
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "Return only the marker from the attached synthetic file.",
                                    },
                                    {"type": "input_file", "file_id": file_id},
                                ],
                            }
                        ],
                        "stream": False,
                    }
                ),
                content_type="application/json",
            )
        )
        if "NEW-API-FILE-OK" not in _extract_chat_content(chat):
            raise ProbeError("semantic_mismatch")
        return CheckResult(
            "files",
            "PASS",
            SUCCESS_CODES["files"],
            {"uploaded": True, "referenced": True},
        )
    except ProbeError as exc:
        return _failed("files", exc)


def check_vision(
    target: TargetConfig, transport: Transport = http_request
) -> CheckResult:
    try:
        image_url = "data:image/png;base64," + base64.b64encode(
            make_test_png((0, 0, 255))
        ).decode("ascii")
        payload = decode_json(
            transport(
                target,
                "POST",
                "/v1/chat/completions",
                body=_json_body(
                    {
                        "model": CHAT_MODEL,
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "Name only the dominant uppercase English color.",
                                    },
                                    {
                                        "type": "image_url",
                                        "image_url": {"url": image_url},
                                    },
                                ],
                            }
                        ],
                        "stream": False,
                    }
                ),
                content_type="application/json",
            )
        )
        if _extract_chat_content(payload).strip().upper() != "BLUE":
            raise ProbeError("semantic_mismatch")
        return CheckResult(
            "vision",
            "PASS",
            SUCCESS_CODES["vision"],
            {"image_uploaded": True, "image_understood": True},
        )
    except ProbeError as exc:
        return _failed("vision", exc)


def _check_image(
    name: str,
    path: str,
    target: TargetConfig,
    *,
    body: bytes,
    content_type: str,
    transport: Transport,
    reject_image: bytes | None = None,
) -> CheckResult:
    try:
        details, image = decode_image_result(
            decode_json(
                transport(
                    target,
                    "POST",
                    path,
                    body=body,
                    content_type=content_type,
                    timeout=180,
                )
            )
        )
        if reject_image is not None and image == reject_image:
            raise ProbeError("semantic_mismatch")
        return CheckResult(name, "PASS", SUCCESS_CODES[name], details)
    except ProbeError as exc:
        return _failed(name, exc)


def check_image_generation(
    target: TargetConfig, transport: Transport = http_request
) -> CheckResult:
    return _check_image(
        "image_generation",
        "/v1/images/generations",
        target,
        body=_json_body(
            {
                "model": IMAGE_MODEL,
                "prompt": "A small blue square centered on a white background.",
                "size": "1024x1024",
                "response_format": "b64_json",
            }
        ),
        content_type="application/json",
        transport=transport,
    )


def check_image_edit(
    target: TargetConfig, transport: Transport = http_request
) -> CheckResult:
    source = make_test_png((0, 0, 255))
    content_type, body = encode_multipart(
        fields={
            "model": IMAGE_MODEL,
            "prompt": "Change the blue square to red.",
            "response_format": "b64_json",
        },
        files={"image": ("source.png", "image/png", source)},
    )
    return _check_image(
        "image_edit",
        "/v1/images/edits",
        target,
        body=body,
        content_type=content_type,
        transport=transport,
        reject_image=source,
    )


def check_image_variation(
    target: TargetConfig, transport: Transport = http_request
) -> CheckResult:
    content_type, body = encode_multipart(
        fields={"model": IMAGE_MODEL, "response_format": "b64_json"},
        files={
            "image": ("source.png", "image/png", make_test_png((0, 0, 255)))
        },
    )
    return _check_image(
        "image_variation",
        "/v1/images/variations",
        target,
        body=body,
        content_type=content_type,
        transport=transport,
    )


def make_english_wav() -> bytes:
    try:
        packed = base64.b85decode(ENGLISH_WAV_LZMA_B85.encode("ascii"))
        audio = lzma.decompress(packed)
        with wave.open(io.BytesIO(audio), "rb") as source:
            if (
                source.getcomptype() != "NONE"
                or source.getnchannels() != 1
                or source.getsampwidth() != 2
                or source.getframerate() not in {16000, 22050}
                or source.getnframes() <= source.getframerate() // 2
                or source.getnframes() > source.getframerate() * 10
            ):
                raise ProbeError("invalid_media")
            frames = source.readframes(source.getnframes() + 1)
            if len(frames) != source.getnframes() * 2 or source.readframes(1):
                raise ProbeError("invalid_media")
    except (ValueError, lzma.LZMAError, EOFError, wave.Error) as exc:
        raise ProbeError("invalid_media") from exc
    if len(audio) > MAX_REQUEST_BYTES:
        raise ProbeError("invalid_media")
    return audio


def validate_audio(audio: bytes, media_type: str) -> dict[str, object]:
    if (
        not isinstance(audio, bytes)
        or not audio
        or len(audio) > MAX_RESPONSE_BYTES
        or media_type.split(";", 1)[0].strip().lower() != "audio/mpeg"
    ):
        raise ProbeError("invalid_media")
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise ProbeError("invalid_media")
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_type,codec_name,sample_rate,channels:format=duration",
                "-of",
                "json",
                "pipe:0",
            ],
            input=audio,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeError("invalid_media") from exc
    if completed.returncode != 0 or len(completed.stdout) > MAX_JSON_BYTES:
        raise ProbeError("invalid_media")
    try:
        metadata = json.loads(completed.stdout.decode("utf-8"))
        streams = metadata["streams"]
        stream = streams[0]
        duration = float(metadata["format"]["duration"])
        sample_rate = int(stream["sample_rate"])
        channels = int(stream["channels"])
        if (
            not isinstance(streams, list)
            or not isinstance(stream, dict)
            or stream.get("codec_type") != "audio"
            or stream.get("codec_name") != "mp3"
            or sample_rate < 8000
            or sample_rate > 192000
            or channels < 1
            or channels > 8
            or duration <= 0
            or duration > 120
        ):
            raise ProbeError("invalid_media")
    except (KeyError, IndexError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProbeError("invalid_media") from exc
    return {
        "media_type": "audio/mpeg",
        "bytes": len(audio),
        "codec": "mp3",
        "sample_rate": sample_rate,
        "channels": channels,
    }


def _extract_audio_text(response: HttpResponse) -> str:
    payload = decode_json(response)
    if set(payload) != {"text"} or not isinstance(payload["text"], str):
        raise ProbeError("relay")
    text = " ".join(payload["text"].split())
    if not text or len(text) > 4096:
        raise ProbeError("relay")
    return text


def check_audio_speech(
    target: TargetConfig, transport: Transport = http_request
) -> CheckResult:
    try:
        response = require_success(
            transport(
                target,
                "POST",
                "/v1/audio/speech",
                body=_json_body(
                    {
                        "model": TTS_MODEL,
                        "input": "Today capability test",
                        "voice": "alloy",
                        "response_format": "mp3",
                    }
                ),
                content_type="application/json",
                timeout=180,
            )
        )
        details = validate_audio(
            response.body, response.headers.get("content-type", "")
        )
        return CheckResult(
            "audio_speech", "PASS", SUCCESS_CODES["audio_speech"], details
        )
    except ProbeError as exc:
        return _failed("audio_speech", exc)


def _request_audio_text(
    target: TargetConfig,
    path: str,
    audio: bytes,
    transport: Transport,
) -> str:
    content_type, body = encode_multipart(
        fields={"model": TRANSCRIPTION_MODEL},
        files={"file": ("english-fixture.wav", "audio/wav", audio)},
    )
    return _extract_audio_text(
        transport(
            target,
            "POST",
            path,
            body=body,
            content_type=content_type,
            timeout=180,
        )
    )


def _has_english_markers(text: str) -> bool:
    folded = text.casefold()
    return (
        "today" in folded
        and any(marker in folded for marker in ("capability", "ability"))
        and any(marker in folded for marker in ("test", "assessment", "evaluation"))
    )


def check_audio_transcription(
    target: TargetConfig,
    audio: bytes,
    transport: Transport = http_request,
) -> tuple[CheckResult, str | None]:
    try:
        text = _request_audio_text(
            target, "/v1/audio/transcriptions", audio, transport
        )
        if not _has_english_markers(text):
            raise ProbeError("semantic_mismatch")
        return (
            CheckResult(
                "audio_transcription",
                "PASS",
                SUCCESS_CODES["audio_transcription"],
                {"text_present": True, "expected_marker_present": True},
            ),
            text,
        )
    except ProbeError as exc:
        return _failed("audio_transcription", exc), None


def check_audio_translation(
    target: TargetConfig,
    audio: bytes,
    transport: Transport = http_request,
) -> CheckResult:
    try:
        text = _request_audio_text(target, "/v1/audio/translations", audio, transport)
        if not _has_english_markers(text):
            raise ProbeError("semantic_mismatch")
        return CheckResult(
            "audio_translation",
            "PASS",
            SUCCESS_CODES["audio_translation"],
            {"text_present": True, "english_markers_present": True},
        )
    except ProbeError as exc:
        return _failed("audio_translation", exc)


def check_audio_translation_composed(
    target: TargetConfig,
    transcription: str | None,
    transport: Transport = http_request,
) -> CheckResult:
    if transcription is None:
        return CheckResult(
            "audio_translation_composed",
            "FAIL",
            "dependency_failed",
            {"dependency": "audio_transcription"},
        )
    try:
        payload = decode_json(
            transport(
                target,
                "POST",
                "/v1/chat/completions",
                body=_json_body(
                    {
                        "model": CHAT_MODEL,
                        "messages": [
                            {
                                "role": "user",
                                "content": "Translate the following synthetic transcript to Simplified Chinese:\n"
                                + transcription,
                            }
                        ],
                        "stream": False,
                    }
                ),
                content_type="application/json",
            )
        )
        translated = _extract_chat_content(payload)
        if (
            not any("\u4e00" <= character <= "\u9fff" for character in translated)
            or transcription.casefold() in translated.casefold()
        ):
            raise ProbeError("semantic_mismatch")
        return CheckResult(
            "audio_translation_composed",
            "PASS",
            SUCCESS_CODES["audio_translation_composed"],
            {"text_present": True, "chinese_present": True},
        )
    except ProbeError as exc:
        return _failed("audio_translation_composed", exc)


def read_client_token(database_path: Path, now: int) -> str:
    database = sqlite3.connect(
        f"file:{database_path.as_posix()}?mode=ro",
        uri=True,
        timeout=30,
    )
    try:
        row = database.execute(
            """
            SELECT key FROM tokens
            WHERE status = 1
              AND (expired_time = -1 OR expired_time > ?)
              AND (unlimited_quota = 1 OR remain_quota > 0)
            ORDER BY id LIMIT 1
            """,
            (now,),
        ).fetchone()
    finally:
        database.close()
    if row is None or not isinstance(row[0], str) or not row[0]:
        raise ProbeError("credential_unavailable")
    return row[0] if row[0].startswith("sk-") else f"sk-{row[0]}"


def _validate_detail_value(key: str, value: object) -> None:
    if key in BOOLEAN_DETAIL_KEYS:
        if value is not True:
            raise ProbeError("invalid_report")
        return
    if key in COUNT_DETAIL_KEYS:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ProbeError("invalid_report")
        if value < 1 or value > 1_000_000:
            raise ProbeError("invalid_report")
        return
    allowed = {
        "media_type": {"image/png", "audio/mpeg"},
        "codec": {"mp3"},
        "dependency": set(EXPECTED_CHECKS),
    }
    if key not in allowed or value not in allowed[key]:
        raise ProbeError("invalid_report")


def validate_report(report: dict[str, object]) -> dict[str, object]:
    if not isinstance(report, dict) or set(report) != {
        "schema_version",
        "checked_at",
        "overall",
        "checks",
    }:
        raise ProbeError("invalid_report")
    if report["schema_version"] != 1:
        raise ProbeError("invalid_report")
    checked_at = report["checked_at"]
    if (
        not isinstance(checked_at, str)
        or len(checked_at) > 32
        or not checked_at.endswith("Z")
    ):
        raise ProbeError("invalid_report")
    checks = report["checks"]
    if not isinstance(checks, list) or len(checks) != len(EXPECTED_CHECKS):
        raise ProbeError("invalid_check_order")
    names = tuple(
        item.get("name") if isinstance(item, dict) else None for item in checks
    )
    if names != EXPECTED_CHECKS:
        raise ProbeError("invalid_check_order")

    statuses: list[str] = []
    for item in checks:
        if not isinstance(item, dict) or set(item) != {
            "name",
            "status",
            "code",
            "details",
        }:
            raise ProbeError("invalid_report")
        name = item["name"]
        status = item["status"]
        code = item["code"]
        details = item["details"]
        if status not in {"PASS", "FAIL"} or not isinstance(details, dict):
            raise ProbeError("invalid_report")
        if status == "PASS":
            if code != SUCCESS_CODES[name] or set(details) != PASS_DETAIL_KEYS[code]:
                raise ProbeError("invalid_report")
        else:
            if code not in FAILURE_CODES:
                raise ProbeError("invalid_report")
            expected_keys = {"dependency"} if code == "dependency_failed" else set()
            if set(details) != expected_keys:
                raise ProbeError("invalid_report")
        for key, value in details.items():
            _validate_detail_value(key, value)
        statuses.append(status)

    expected_overall = "PASS" if all(status == "PASS" for status in statuses) else "FAIL"
    if report["overall"] != expected_overall:
        raise ProbeError("invalid_report")
    return report


def serialize_report(report: dict[str, object]) -> str:
    validate_report(report)
    encoded = json.dumps(
        report,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded.encode("utf-8")) > MAX_REPORT_BYTES:
        raise ProbeError("report_too_large")
    return encoded


def run_matrix(
    target: TargetConfig, transport: Transport = http_request
) -> list[CheckResult]:
    audio = make_english_wav()
    results = [
        check_models(target, transport),
        check_chat_nonstream(target, transport),
        check_chat_stream(target, transport),
        check_responses_nonstream(target, transport),
        check_responses_stream(target, transport),
        check_files(target, transport),
        check_vision(target, transport),
        check_image_generation(target, transport),
        check_image_edit(target, transport),
        check_image_variation(target, transport),
        check_audio_speech(target, transport),
    ]
    transcription, text = check_audio_transcription(target, audio, transport)
    results.extend(
        [
            transcription,
            check_audio_translation(target, audio, transport),
            check_audio_translation_composed(target, text, transport),
        ]
    )
    if tuple(result.name for result in results) != EXPECTED_CHECKS:
        raise ProbeError("invalid_check_order")
    return results


def build_report(
    results: list[CheckResult], *, checked_at: str | None = None
) -> dict[str, object]:
    if tuple(result.name for result in results) != EXPECTED_CHECKS:
        raise ProbeError("invalid_check_order")
    report = {
        "schema_version": 1,
        "checked_at": checked_at
        or datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
        "overall": "PASS"
        if all(result.status == "PASS" for result in results)
        else "FAIL",
        "checks": [
            {
                "name": result.name,
                "status": result.status,
                "code": result.code,
                "details": dict(result.details),
            }
            for result in results
        ],
    }
    return validate_report(report)


def atomic_write(output: Path, payload: bytes) -> None:
    if not isinstance(payload, bytes) or len(payload) > MAX_REPORT_BYTES:
        raise ProbeError("output_write_failed")
    parent = output.parent
    try:
        if parent.is_symlink() or not parent.is_dir():
            raise ProbeError("output_write_failed")
        if output.exists() and (output.is_symlink() or not output.is_file()):
            raise ProbeError("output_write_failed")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".tmp", dir=parent
        )
        temporary = Path(temporary_name)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, output)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
    except ProbeError:
        raise
    except OSError as exc:
        raise ProbeError("output_write_failed") from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-real-api", action="store_true")
    parser.add_argument("--root", type=Path, default=EXPECTED_ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.allow_real_api:
        return 2
    try:
        if args.root != EXPECTED_ROOT:
            raise ProbeError("root_invalid")
        token = read_client_token(
            args.root / "data" / "new-api" / "one-api.db", int(time.time())
        )
        results = run_matrix(TargetConfig(NEW_API_BASE_URL, token))
        report = build_report(results)
        payload = serialize_report(report)
        if args.output is not None:
            if args.output != Path("/tmp/new-api-multimodal-report.json"):
                raise ProbeError("output_invalid")
            atomic_write(args.output, payload.encode("utf-8"))
        if args.json:
            print(payload)
        else:
            print(f"new_api_multimodal={report['overall']}")
        return 0 if report["overall"] == "PASS" else 1
    except (ProbeError, sqlite3.Error):
        print("new_api_multimodal=ERROR", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
