<?php
declare(strict_types=1);

namespace Web;

use Shared\Money\Price;

// Classless bootstrap wiring — its cross-repo references surface as
// CrossRepoFileRef (File -> Class) edges, and the namespaced-function call
// as a CrossRepoCalls edge from the (global) function web_boot.
function web_boot(): Price
{
    $seed = \Shared\Util\money_round(1999, 2);

    return new Price($seed);
}
