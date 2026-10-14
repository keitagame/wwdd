#!/usr/bin/perl
# wplace風 世界地図ピクセルキャンバス (Perl CGI + SSE) -- クールタイムなし
#
#   GET  place.cgi?a=tile&x=TX&y=TY    タイル(1000x1000, 1ピクセル=1バイトの色番号)を gzip で返す
#                                       まだ何も描かれていないタイルは 204 を返す
#   GET  place.cgi?a=seq               現在の更新シーケンス番号
#   POST place.cgi  a=place&x=&y=&c=   ピクセルを置く (x,y は世界ピクセル座標 0..2047999, c=0 は消しゴム)
#   GET  place.cgi?a=stream&from=N     SSE ストリーム (N以降の更新を配信。from=now なら現在以降だけ)
#
# 世界は Web Mercator のズーム11タイル (2048x2048 枚) を 1タイル 1000x1000 ピクセルに分割したもの。
#   世界ピクセル座標 gx = tx*1000 + px  (全体で 2,048,000 x 2,048,000)
#
# 状態はファイルで共有する(CGIはリクエストごとに別プロセスのため)
#   data/tiles/TX_TY.bin  1000*1000 バイト。描かれたタイルだけ作られる(スパースファイル)。色番号 0=透明, 1..64=パレット
#   data/log9.bin         更新ログ (1件=9バイト gx(32bit),gy(32bit),c(8bit))。ログの件数がシーケンス番号
#   ※ 旧バージョンの board.bin / log5.bin とは互換性なし(使われません)

use strict;
use warnings;
use Fcntl qw(:flock :DEFAULT SEEK_SET);
use Time::HiRes ();
use IO::Compress::Gzip qw(gzip $GzipError);

my $TS    = 1000;                      # 1タイルの一辺(ピクセル)
my $NT    = 2048;                      # 1辺のタイル数 (ズーム11)
my $WORLD = $TS * $NT;                 # 世界の一辺(ピクセル) = 2,048,000
my $NCOL  = 65;                        # 0=透明(消しゴム) + パレット64色
my $DIR   = $ENV{PLACE_DATA} || './data';
my $TILEDIR = "$DIR/tiles";
my $LOG   = "$DIR/log9.bin";
my $LOCK  = "$DIR/lock";
my $REC   = 9;
my $MAX_LIFETIME = 300;                # 1接続の最大秒数(切れてもクライアントが自動再接続)

# ---------- 共通 ----------
sub parse_params {
    my $q = $ENV{QUERY_STRING} // '';
    if (($ENV{REQUEST_METHOD} // '') eq 'POST') {
        my $len = $ENV{CONTENT_LENGTH} || 0;
        $len = 4096 if $len > 4096;
        read(STDIN, my $body, $len);
        $q .= '&' . ($body // '');
    }
    my %p;
    for my $pair (split /&/, $q) {
        my ($k, $v) = split /=/, $pair, 2;
        next unless defined $k && length $k;
        $v //= '';
        for ($k, $v) { tr/+/ /; s/%([0-9A-Fa-f]{2})/chr hex $1/ge; }
        $p{$k} = $v;
    }
    return \%p;
}

sub init_storage {
    return if -d $TILEDIR && -e $LOG && -e $LOCK;     # 通常はここで即return
    mkdir $DIR unless -d $DIR;
    open my $l, '>>', $LOCK or die "lock: $!";
    flock($l, LOCK_EX);
    mkdir $TILEDIR unless -d $TILEDIR;
    unless (-e $LOG) {
        open my $g, '>', $LOG or die "log: $!";
        close $g;
    }
    close $l;
}

sub fail {
    my ($status, $msg) = @_;
    print "Status: $status\nContent-Type: text/plain\n\n$msg\n";
    exit 0;
}

# ---------- 現在のシーケンス番号 ----------
sub do_seq {
    my $seq = int(((-s $LOG) // 0) / $REC);
    print "Content-Type: text/plain\nCache-Control: no-store\n\n$seq\n";
}

# ---------- タイル ----------
# 1000x1000 バイトをそのまま gzip して返す。ETag で再検証(304)できる
sub do_tile {
    my $p = shift;
    my ($tx, $ty) = @{$p}{qw(x y)};
    for ($tx, $ty) { fail(400, 'bad params') unless defined && /^\d{1,4}$/; }
    ($tx, $ty) = (int $tx, int $ty);
    fail(400, 'out of range') if $tx >= $NT || $ty >= $NT;

    my $path = "$TILEDIR/${tx}_${ty}.bin";
    my @st = Time::HiRes::stat($path);
    unless (@st) {                                    # まだ誰も描いていないタイル
        print "Status: 204 No Content\nCache-Control: no-cache\n\n";
        return;
    }
    my $etag = sprintf('"%d-%d"', $st[7], int($st[9] * 1e6));
    if (($ENV{HTTP_IF_NONE_MATCH} // '') eq $etag) {
        print "Status: 304 Not Modified\nETag: $etag\nCache-Control: no-cache\n\n";
        return;
    }

    open my $f, '<', $path or fail(500, 'tile');
    binmode $f;
    local $/;
    my $raw = <$f>;
    close $f;
    $raw //= '';
    $raw .= "\0" x ($TS * $TS - length $raw) if length $raw < $TS * $TS;   # 末尾が未書き込みなら0で補う
    $raw = substr($raw, 0, $TS * $TS);

    my $gz;
    gzip(\$raw => \$gz, -Level => 3) or fail(500, "gzip: $GzipError");

    $| = 1;
    binmode STDOUT;
    print "Content-Type: application/octet-stream\n",
          "Content-Encoding: gzip\n",
          "ETag: $etag\n",
          "Cache-Control: no-cache\n",
          "Content-Length: ", length($gz), "\n\n";
    print $gz;
}

# ---------- ピクセルを置く(クールタイムなし) ----------
sub do_place {
    fail(405, 'POST only') unless ($ENV{REQUEST_METHOD} // '') eq 'POST';
    my $p = shift;
    my ($x, $y, $c) = @{$p}{qw(x y c)};
    for ($x, $y, $c) { fail(400, 'bad params') unless defined && /^\d{1,7}$/; }
    ($x, $y, $c) = (int $x, int $y, int $c);
    fail(400, 'out of range') if $x >= $WORLD || $y >= $WORLD || $c >= $NCOL;

    my ($tx, $px) = (int($x / $TS), $x % $TS);
    my ($ty, $py) = (int($y / $TS), $y % $TS);

    open my $l, '>>', $LOCK or fail(500, 'lock');
    flock($l, LOCK_EX);

    # タイルは初めて描かれたときに作る。書き込み位置までは自動的に0で埋まる(スパース)
    sysopen(my $b, "$TILEDIR/${tx}_${ty}.bin", O_RDWR | O_CREAT, 0644) or fail(500, 'tile');
    binmode $b;
    seek $b, $py * $TS + $px, SEEK_SET;
    print $b chr($c);
    close $b;

    open my $g, '>>', $LOG or fail(500, 'log');
    binmode $g;
    print $g pack('NNC', $x, $y, $c);
    close $g;

    close $l;
    print "Status: 204 No Content\n\n";
}

# ---------- SSE ----------
sub do_stream {
    my $p = shift;
    my $from = $ENV{HTTP_LAST_EVENT_ID};              # 自動再接続時はこちらが優先
    $from = $p->{from} unless defined $from && $from =~ /^\d+$/;
    $from = 0 unless defined $from && $from =~ /^\d+$/;      # (from=now は下で処理)

    $| = 1;
    binmode STDOUT;
    $SIG{PIPE} = sub { exit 0 };                      # クライアント切断で終了
    print "Content-Type: text/event-stream\n",
          "Cache-Control: no-cache\n",
          "X-Accel-Buffering: no\n\n",
          "retry: 1000\n\n";

    my $avail0 = int(((-s $LOG) // 0) / $REC);
    my $seq = ($p->{from} // '') eq 'now' && !defined $ENV{HTTP_LAST_EVENT_ID} ? $avail0 : $from + 0;
    $seq = $avail0 if $seq > $avail0;                 # ログが作り直された場合の保険

    my $start = time;
    my $last  = time;
    while (time - $start < $MAX_LIFETIME) {
        my $avail = int(((-s $LOG) // 0) / $REC);
        if ($avail > $seq) {
            my $n = $avail - $seq;
            $n = 2000 if $n > 2000;
            open my $f, '<', $LOG or last;
            binmode $f;
            seek $f, $seq * $REC, SEEK_SET;
            read $f, my $buf, $n * $REC;
            close $f;
            my @pts;
            for my $i (0 .. $n - 1) {
                my ($x, $y, $c) = unpack 'NNC', substr($buf, $i * $REC, $REC);
                push @pts, "$x,$y,$c";
            }
            $seq += $n;
            print "id: $seq\ndata: ", join(';', @pts), "\n\n";
            $last = time;
        } else {
            if (time - $last >= 15) {                 # プロキシに切られないためのハートビート
                print ": ping\n\n";
                $last = time;
            }
            select(undef, undef, undef, 0.1);
        }
    }
}

# ---------- ディスパッチ ----------
init_storage();
my $p = parse_params();
my $a = $p->{a} // '';

if    ($a eq 'tile')   { do_tile($p) }
elsif ($a eq 'seq')    { do_seq() }
elsif ($a eq 'place')  { do_place($p) }
elsif ($a eq 'stream') { do_stream($p) }
else                   { fail(400, 'unknown action') }
