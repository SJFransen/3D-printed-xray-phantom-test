# Troubleshooting content

Keep explanations and media here so the application can serve them locally.
Deploy this directory alongside `phantom_qa`; reading content requires no
external internet connection.

Feature folders: `line-pair`, `low-contrast`, `uniformity`, `dynamic-range`,
`dimensions`, and `field-alignment`.

Example:

```text
solutions/line-pair/step 3/explanation.txt
solutions/line-pair/step 3/01.png
solutions/line-pair/step 3/demo.mp4
```

Steps sort numerically: step 2 precedes step 10. Multiple `.txt` files within
a step sort by filename. Use UTF-8. Text is displayed literally, without HTML.
`operator.txt` appears with common text in Auto Analyze. Analyze additionally
shows `technician.txt` and `physicist.txt`. Other text files appear in both
workflows. There is no user-view selector.

Write media placeholders wherever the media belongs in the explanation:

```text
Check that the measuring boxes lie on the printed patterns.
[image 01]
This picture shows the expected placement. Watch the positioning example:
[video demo.mp4]
Then review the result again.
```

`[image 01]` matches `01.png`, `01.jpg`, `01.jpeg`, or `image 01.png`.
Use an exact filename, for example `[image placement.png]`, to disambiguate.
`[video]` works when exactly one video exists in that step. Otherwise use
`[video filename.mp4]`. Media must be in the same step directory.

Images: PNG, JPG, JPEG. Videos: MP4, WebM, OGV, MOV. Browser playback depends
on the codec; MP4 with H.264 or WebM is preferable. Videos do not autoplay or
preload. Missing or
ambiguous media produces a written fallback rather than hiding the explanation.

Multiple solution steps start collapsed and can be opened independently.
The application links solutions to affected features, including incomplete
measurements. A pass does not create a troubleshooting task. The initial text
covers acquisition/position checks and escalation; add approved local
troubleshooting content and media as they become available.

The existing administrator credential is still required for validation.
