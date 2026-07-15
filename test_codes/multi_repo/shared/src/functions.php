<?php
declare(strict_types=1);

namespace Shared\Util;

function money_round(int $cents, int $precision): int
{
    $factor = 10 ** $precision;

    return intdiv($cents + intdiv($factor, 2), $factor) * $factor;
}
